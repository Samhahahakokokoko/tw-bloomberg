"""
alpha_portfolio_engine.py — Multi-Alpha 組合引擎（機構級）

整合四個獨立 Alpha：
  Alpha1: momentum — 動能（20日報酬 + 量能 + 外資）
  Alpha2: value    — 價值存股（殖利率 + PE + EPS 穩定）
  Alpha3: chip     — 籌碼法人（外資 + 投信 + 主力）
  Alpha4: breakout — 技術突破（突破壓力 + KD + MA）

組合邏輯：
  1. 各 Alpha 獨立計算 score（0~100）
  2. 依 IC 動態加權合成 composite_score
  3. 依 regime 調整各 Alpha 權重乘數
  4. No-Trade：多 Alpha 分歧 > 0.3 → 不交易
  5. 輸出 AlphaPortfolioResult（含詳細明細）

No-Trade 定義：
  divergence = std(scores) / mean(scores)
  divergence > 0.3 → 訊號不一致，禁止交易
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 常數 ─────────────────────────────────────────────────────────────────────

DIVERGENCE_THRESHOLD = 0.3   # 分歧閾值
MIN_SCORE_TO_BUY     = 60.0  # composite score >= 60 才產生 BUY
MAX_SCORE_TO_SELL    = 40.0  # composite score <= 40 才產生 SELL


# ── Alpha 計算函式 ────────────────────────────────────────────────────────────

def _momentum_score(data: dict) -> float:
    """動能 Alpha（0~100）"""
    s = 50.0
    # 20日報酬
    mom = float(data.get("momentum_20d", 1.0))
    s += (mom - 1.0) * 200   # +5% → +10分
    # 外資連買
    fb = int(data.get("foreign_buy_days", 0))
    s += min(fb * 3.0, 20.0) if fb > 0 else max(fb * 2.0, -15.0)
    # 量比
    vr = float(data.get("volume_ratio", 1.0))
    s += (vr - 1.0) * 15 if vr > 1 else 0.0
    return float(np.clip(s, 0, 100))


def _value_score(data: dict) -> float:
    """價值 Alpha（0~100）"""
    s = 50.0
    dy  = float(data.get("dividend_yield", 0))
    pe  = float(data.get("pe_ratio", 20))
    eps = float(data.get("eps_stability", 0.5))
    # 殖利率 0~8% → 0~24分
    s += min(dy * 3.0, 24.0)
    # 本益比（低 PE 加分）
    if 0 < pe < 10:
        s += 20
    elif 10 <= pe < 15:
        s += 12
    elif pe > 30:
        s -= 10
    # EPS 穩定度 0~1 → 0~20分
    s += eps * 20
    return float(np.clip(s, 0, 100))


def _chip_score(data: dict) -> float:
    """籌碼 Alpha（0~100）"""
    s = 50.0
    fn = float(data.get("foreign_net", 0))
    tn = float(data.get("trust_net", 0))
    dn = float(data.get("dealer_net", 0))
    cc = float(data.get("chip_concentration", 50))
    # 外資 / 投信 / 自營（各貢獻最多 15 分）
    s += min(fn / 500, 15.0) if fn > 0 else max(fn / 200, -10.0)
    s += min(tn / 100, 12.0) if tn > 0 else max(tn / 80, -8.0)
    s += min(dn / 80,   8.0) if dn > 0 else max(dn / 60, -5.0)
    # 籌碼集中度 50~100 → 0~15分
    s += max(0, (cc - 50) * 0.3)
    return float(np.clip(s, 0, 100))


def _breakout_score(data: dict) -> float:
    """突破 Alpha（0~100）"""
    s = 50.0
    close  = float(data.get("close", 100))
    ma20   = float(data.get("ma20", close))
    ma60   = float(data.get("ma60", close))
    kd_k   = float(data.get("k", 50))
    kd_d   = float(data.get("d", 50))
    boll_b = float(data.get("boll_b", 0.5))
    golden = int(data.get("macd_golden", 0))

    if close > ma20 > ma60:
        s += 15
    if close > ma20:
        s += 8
    if kd_k > kd_d and kd_k > 50:
        s += 10
    if golden:
        s += 12
    if 0.7 <= boll_b <= 1.0:
        s += 8
    elif boll_b > 1.0:
        s -= 5

    return float(np.clip(s, 0, 100))


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class AlphaScore:
    name:   str
    score:  float    # 0~100
    weight: float    # IC 動態權重
    weighted_score: float  # score × weight

    def to_dict(self) -> dict:
        return {
            "name":           self.name,
            "score":          round(self.score, 2),
            "weight":         round(self.weight, 4),
            "weighted_score": round(self.weighted_score, 2),
        }


@dataclass
class AlphaPortfolioResult:
    """Multi-Alpha 組合輸出結果"""
    stock_id:        str
    composite_score: float       # 加權後綜合分 0~100
    signal:          str         # "buy" / "sell" / "hold" / "no_trade"
    divergence:      float       # Alpha 分歧度
    no_trade:        bool        # 分歧 > threshold → 不交易
    no_trade_reason: str

    alphas:          list[AlphaScore]
    regime:          str
    regime_scale:    float       # 盤態倉位乘數
    final_position_pct: float    # 建議倉位（考慮 regime scale）

    def to_dict(self) -> dict:
        return {
            "stock_id":          self.stock_id,
            "composite_score":   round(self.composite_score, 2),
            "signal":            self.signal,
            "divergence":        round(self.divergence, 3),
            "no_trade":          self.no_trade,
            "no_trade_reason":   self.no_trade_reason,
            "regime":            self.regime,
            "regime_scale":      round(self.regime_scale, 3),
            "final_position_pct":round(self.final_position_pct, 3),
            "alphas":            [a.to_dict() for a in self.alphas],
        }


# ── 主引擎 ───────────────────────────────────────────────────────────────────

class AlphaPortfolioEngine:
    """
    Multi-Alpha 組合引擎。

    使用方式：
        engine = AlphaPortfolioEngine()
        result = engine.evaluate(data, regime="bull")

    data 欄位（和 StrategyEngine 相容）：
      stock_id, momentum_20d, foreign_buy_days, volume_ratio,
      dividend_yield, pe_ratio, eps_stability,
      foreign_net, trust_net, dealer_net, chip_concentration,
      close, ma20, ma60, k, d, boll_b, macd_golden
    """

    # 預設 Alpha 等權（實際使用時由 IC 動態加權）
    DEFAULT_ALPHA_WEIGHTS: dict[str, float] = {
        "momentum": 0.30,
        "value":    0.20,
        "chip":     0.30,
        "breakout": 0.20,
    }

    # 各盤態的 Alpha 權重調整係數
    REGIME_ALPHA_MULT: dict[str, dict[str, float]] = {
        "bull":     {"momentum": 1.5, "value": 0.7,  "chip": 1.2, "breakout": 1.3},
        "bear":     {"momentum": 0.4, "value": 1.5,  "chip": 0.8, "breakout": 0.4},
        "sideways": {"momentum": 0.8, "value": 1.2,  "chip": 1.3, "breakout": 0.8},
        "volatile": {"momentum": 0.5, "value": 1.3,  "chip": 0.7, "breakout": 0.5},
        "unknown":  {"momentum": 0.7, "value": 1.2,  "chip": 0.8, "breakout": 0.7},
    }

    # 盤態倉位乘數
    REGIME_POSITION_SCALE: dict[str, float] = {
        "bull":     1.00,
        "bear":     0.45,
        "sideways": 0.75,
        "volatile": 0.40,
        "unknown":  0.50,
    }

    def __init__(
        self,
        ic_weights: Optional[dict[str, float]] = None,
        divergence_threshold: float = DIVERGENCE_THRESHOLD,
        base_position_pct: float = 0.10,
    ):
        self.ic_weights            = ic_weights or {}
        self.divergence_threshold  = divergence_threshold
        self.base_position_pct     = base_position_pct

    def set_ic_weights(self, weights: dict[str, float]) -> None:
        """每日更新因子 IC 權重（由 DynamicWeightEngine 傳入）"""
        self.ic_weights = weights

    # ── 評估單股 ──────────────────────────────────────────────────────────────

    def evaluate(self, data: dict, regime: str = "unknown") -> AlphaPortfolioResult:
        stock_id = str(data.get("stock_id", "????"))

        # ── 計算各 Alpha score ─────────────────────────────────────────────
        raw_scores = {
            "momentum": _momentum_score(data),
            "value":    _value_score(data),
            "chip":     _chip_score(data),
            "breakout": _breakout_score(data),
        }

        # ── Alpha 動態加權（IC → regime 調整）────────────────────────────
        base_weights = dict(self.DEFAULT_ALPHA_WEIGHTS)

        # 若有 IC 動態權重，用來修正預設等權
        if self.ic_weights:
            for alpha_name in base_weights:
                # 取對應 alpha 的 IC 信號（透過 factor_group 映射）
                ic_signal = self.ic_weights.get(alpha_name, base_weights[alpha_name])
                base_weights[alpha_name] = max(0.0, ic_signal)

        # Regime 調整
        regime_mult = self.REGIME_ALPHA_MULT.get(regime, self.REGIME_ALPHA_MULT["unknown"])
        for name in base_weights:
            base_weights[name] *= regime_mult.get(name, 1.0)

        # 歸一化
        total_w = sum(base_weights.values())
        if total_w > 0:
            base_weights = {k: v / total_w for k, v in base_weights.items()}

        # ── 建構 AlphaScore 列表 ───────────────────────────────────────────
        alpha_scores: list[AlphaScore] = []
        for name in ["momentum", "value", "chip", "breakout"]:
            w  = base_weights.get(name, 0.25)
            sc = raw_scores[name]
            alpha_scores.append(AlphaScore(
                name=name, score=sc, weight=w,
                weighted_score=sc * w,
            ))

        # ── 合成 composite score ──────────────────────────────────────────
        composite = sum(a.weighted_score for a in alpha_scores)

        # ── 分歧度計算 ────────────────────────────────────────────────────
        scores_arr = np.array([a.score for a in alpha_scores])
        mean_s     = scores_arr.mean()
        divergence = float(scores_arr.std() / mean_s) if mean_s > 1e-6 else 0.0

        # ── No-Trade 判斷 ─────────────────────────────────────────────────
        no_trade = False
        no_trade_reason = ""
        if divergence > self.divergence_threshold:
            no_trade = True
            scores_str = " / ".join(f"{a.name}={a.score:.0f}" for a in alpha_scores)
            no_trade_reason = (
                f"Alpha 分歧 {divergence:.3f} > {self.divergence_threshold}  "
                f"({scores_str})"
            )

        # ── 訊號決定 ──────────────────────────────────────────────────────
        if no_trade:
            signal = "no_trade"
        elif composite >= MIN_SCORE_TO_BUY:
            signal = "buy"
        elif composite <= MAX_SCORE_TO_SELL:
            signal = "sell"
        else:
            signal = "hold"

        # ── 倉位建議 ──────────────────────────────────────────────────────
        regime_scale = self.REGIME_POSITION_SCALE.get(regime, 0.5)
        final_pct    = self.base_position_pct * regime_scale if not no_trade else 0.0

        logger.debug(
            "[AlphaPort] %s composite=%.1f signal=%s div=%.3f regime=%s",
            stock_id, composite, signal, divergence, regime,
        )

        return AlphaPortfolioResult(
            stock_id=stock_id,
            composite_score=round(composite, 2),
            signal=signal,
            divergence=round(divergence, 3),
            no_trade=no_trade,
            no_trade_reason=no_trade_reason,
            alphas=alpha_scores,
            regime=regime,
            regime_scale=round(regime_scale, 3),
            final_position_pct=round(final_pct, 4),
        )

    # ── 批次評估 ─────────────────────────────────────────────────────────────

    def batch_evaluate(
        self,
        stocks: list[dict],
        regime: str = "unknown",
        min_score: float = 50.0,
    ) -> list[AlphaPortfolioResult]:
        """批次評估多股，按 composite_score 排序，過濾低分"""
        results = [self.evaluate(s, regime=regime) for s in stocks]
        results = [r for r in results if r.composite_score >= min_score and not r.no_trade]
        return sorted(results, key=lambda r: -r.composite_score)

    # ── 與 NoTradeEngine 整合 ─────────────────────────────────────────────────

    def to_no_trade_input(self, result: AlphaPortfolioResult) -> dict:
        """將 AlphaPortfolioResult 轉為 NoTradeEngine 的 ensemble_score 輸入"""
        return {
            "ensemble_score":  result.composite_score,
            "no_trade_alpha":  result.no_trade,
            "divergence":      result.divergence,
        }


# ── 全域單例 ─────────────────────────────────────────────────────────────────

_global_alpha_portfolio: Optional[AlphaPortfolioEngine] = None

def get_alpha_portfolio_engine() -> AlphaPortfolioEngine:
    global _global_alpha_portfolio
    if _global_alpha_portfolio is None:
        _global_alpha_portfolio = AlphaPortfolioEngine()
    return _global_alpha_portfolio
