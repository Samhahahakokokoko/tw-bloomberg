"""
factor_ic_engine.py — 因子 IC / ICIR 評估引擎 v2（機構級）

核心指標：
  IC      = Spearman(factor_t, fwd_return_t)  每日計算
  IC_mean = rolling(IC, 60).mean()
  IC_std  = rolling(IC, 60).std()
  ICIR    = IC_mean / IC_std

因子淘汰規則：
  IC_mean < 0      → weight = 0（反向因子，不使用）
  ICIR    < 0.1    → weight *= 0.5（不穩定因子，減半）
  ICIR    < 0      → weight = 0（IC 均值為負）

輸出：
  get_factor_weights() → {feature_name: weight}  動態權重字典
  可直接 plug 到 DynamicWeightEngine / EnsembleEngine

使用方式：
    engine = FactorICEngine(feat_df, forward_days=5)
    weights = engine.get_factor_weights()   # {factor: weight}
    report  = engine.full_report()          # 完整分析表
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)

# ── 預設評估因子 ──────────────────────────────────────────────────────────────

DEFAULT_FACTORS = [
    "rsi14", "macd_hist", "vol_ratio", "boll_b", "obv_slope5",
    "ma5", "ma20", "ma60", "k", "d",
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    "atr14", "excess_ret", "body_ratio", "hl_ratio", "boll_width",
]

# 因子淘汰門檻
IC_MEAN_MIN   =  0.0    # IC_mean < 0 → weight = 0
ICIR_HALF_THR =  0.10   # ICIR < 0.1 → weight × 0.5
ICIR_ZERO_THR =  0.0    # ICIR < 0   → weight = 0
IC_WINDOW     = 60      # 滾動計算窗口（天）


@dataclass
class FactorReport:
    """單一因子完整 IC 分析結果"""
    factor:        str
    n_obs:         int         # 有效觀測期數
    ic_mean:       float       # 60日滾動 IC 均值（最新）
    ic_std:        float       # 60日滾動 IC 標準差
    icir:          float       # = ic_mean / ic_std
    ic_win_rate:   float       # IC > 0 的比例
    ic_decay_mean: float       # 指數衰退加權 IC 均值
    weight:        float       # 最終動態權重（已套用淘汰規則）
    is_valid:      bool
    elimination_reason: str = ""
    daily_ic:      list[float] = field(default_factory=list)   # IC 時序

    def to_dict(self) -> dict:
        return {
            "factor":             self.factor,
            "n_obs":              self.n_obs,
            "ic_mean":            round(self.ic_mean, 4),
            "ic_std":             round(self.ic_std, 4),
            "icir":               round(self.icir, 3),
            "ic_win_rate":        round(self.ic_win_rate, 3),
            "ic_decay_mean":      round(self.ic_decay_mean, 4),
            "weight":             round(self.weight, 4),
            "is_valid":           self.is_valid,
            "elimination_reason": self.elimination_reason,
        }


class FactorICEngine:
    """
    因子 IC / ICIR 計算引擎。

    feat_df    : FeatureEngine.compute_all() 輸出，必須含 close 欄位
    forward_days: 前瞻報酬天數（預設 5 日）
    ic_window  : 滾動 IC 統計窗口（預設 60 日）
    """

    def __init__(
        self,
        feat_df: pd.DataFrame,
        forward_days: int = 5,
        ic_window: int = IC_WINDOW,
        decay_alpha: float = 0.95,
    ):
        if "close" not in feat_df.columns:
            raise ValueError("feat_df 必須含 close 欄位")

        self.df           = feat_df.copy().reset_index(drop=True)
        self.forward_days = forward_days
        self.ic_window    = ic_window
        self.decay_alpha  = decay_alpha

        # 預計算前瞻報酬（shift 避免未來資料洩漏）
        self.df["_fwd_ret"] = (
            self.df["close"].pct_change(forward_days).shift(-forward_days)
        )

    # ── 單因子每日 IC ─────────────────────────────────────────────────────────

    def compute_daily_ic(self, factor: str) -> pd.Series:
        """
        每日計算 IC：
        day t 的 IC = Spearman corr(factor[t-window:t], fwd_ret[t-window:t])

        回傳長度 = len(df)，前 ic_window 期為 NaN。
        """
        if factor not in self.df.columns:
            return pd.Series([np.nan] * len(self.df))

        ic_vals = [np.nan] * len(self.df)
        x_arr   = self.df[factor].values
        y_arr   = self.df["_fwd_ret"].values
        W       = self.ic_window

        for t in range(W, len(self.df)):
            x_win = x_arr[t - W: t]
            y_win = y_arr[t - W: t]
            mask  = ~(np.isnan(x_win) | np.isnan(y_win))
            if mask.sum() < 10:
                continue
            try:
                rho, _ = scipy_stats.spearmanr(x_win[mask], y_win[mask])
                if not np.isnan(rho):
                    ic_vals[t] = float(rho)
            except Exception:
                pass

        return pd.Series(ic_vals, dtype=float)

    # ── 單因子完整評估 ────────────────────────────────────────────────────────

    def evaluate_factor(self, factor: str) -> FactorReport:
        """計算單因子所有 IC 統計，套用淘汰規則，回傳 FactorReport"""
        daily_ic = self.compute_daily_ic(factor)
        valid    = daily_ic.dropna()
        n        = len(valid)

        # 資料不足
        if n < 20:
            return FactorReport(
                factor=factor, n_obs=n,
                ic_mean=0.0, ic_std=1e-8, icir=0.0, ic_win_rate=0.5,
                ic_decay_mean=0.0, weight=0.0, is_valid=False,
                elimination_reason=f"有效觀測 {n} < 20",
                daily_ic=[],
            )

        ic_mean  = float(valid.mean())
        ic_std   = float(valid.std()) if valid.std() > 1e-8 else 1e-8
        icir     = ic_mean / ic_std
        win_rate = float((valid > 0).mean())

        # 指數衰退加權均值（越新的 IC 佔比越大）
        vals_arr = valid.values
        n_v      = len(vals_arr)
        w_arr    = np.array([self.decay_alpha ** (n_v - 1 - i) for i in range(n_v)])
        w_arr   /= w_arr.sum()
        ic_decay = float(np.dot(w_arr, vals_arr))

        # ── 淘汰規則 ─────────────────────────────────────────────────────
        weight = 1.0
        reasons: list[str] = []

        if ic_mean < IC_MEAN_MIN:
            weight = 0.0
            reasons.append(f"IC均值={ic_mean:.4f}<0（反向因子）")
        if icir < ICIR_ZERO_THR:
            weight = 0.0
            reasons.append(f"ICIR={icir:.3f}<0")
        if weight > 0 and icir < ICIR_HALF_THR:
            weight *= 0.5
            reasons.append(f"ICIR={icir:.3f}<{ICIR_HALF_THR}（不穩定，減半）")

        # 有效因子：以 |ICIR| 作為基礎權重（後續歸一化）
        if weight > 0:
            weight = weight * abs(icir)

        is_valid = weight > 0 and not reasons[:1]  # 第一條 reason 為淘汰理由

        return FactorReport(
            factor=factor,
            n_obs=n,
            ic_mean=round(ic_mean, 4),
            ic_std=round(ic_std, 4),
            icir=round(icir, 3),
            ic_win_rate=round(win_rate, 3),
            ic_decay_mean=round(ic_decay, 4),
            weight=round(weight, 4),
            is_valid=is_valid,
            elimination_reason="; ".join(reasons),
            daily_ic=valid.tolist()[-30:],   # 只保留最近 30 日 IC，節省記憶體
        )

    # ── 批次評估 ─────────────────────────────────────────────────────────────

    def evaluate_all(self, factors: list[str] = DEFAULT_FACTORS) -> list[FactorReport]:
        """批次評估所有因子，依 weight 排序（有效在前）"""
        reports: list[FactorReport] = []
        for f in factors:
            try:
                r = self.evaluate_factor(f)
            except Exception as e:
                logger.warning("[FactorIC] %s 評估失敗: %s", f, e)
                r = FactorReport(
                    factor=f, n_obs=0,
                    ic_mean=0.0, ic_std=1e-8, icir=0.0, ic_win_rate=0.5,
                    ic_decay_mean=0.0, weight=0.0, is_valid=False,
                    elimination_reason=str(e),
                )
            reports.append(r)
            logger.debug("[FactorIC] %-20s IC=%.4f ICIR=%.3f w=%.4f valid=%s",
                         f, r.ic_mean, r.icir, r.weight, r.is_valid)

        return sorted(reports, key=lambda r: -r.weight)

    # ── 動態權重字典（主要輸出）─────────────────────────────────────────────

    def get_factor_weights(
        self,
        factors: list[str] = DEFAULT_FACTORS,
        normalize: bool = True,
    ) -> dict[str, float]:
        """
        主要輸出：{feature_name: weight} 動態權重字典。
        已套用淘汰規則，可直接 plug 到 DynamicWeightEngine。

        normalize=True：所有有效因子權重歸一化，總和 = 1.0
        """
        reports = self.evaluate_all(factors)
        weights = {r.factor: r.weight for r in reports}

        if normalize:
            total = sum(w for w in weights.values() if w > 0)
            if total > 0:
                weights = {
                    k: round(v / total, 6) if v > 0 else 0.0
                    for k, v in weights.items()
                }
        return weights

    # ── 完整報告 ─────────────────────────────────────────────────────────────

    def full_report(
        self,
        factors: list[str] = DEFAULT_FACTORS,
    ) -> dict:
        """回傳完整分析報告（用於 API 輸出）"""
        reports = self.evaluate_all(factors)
        valid_n = sum(1 for r in reports if r.is_valid)
        weights = self.get_factor_weights(factors)

        return {
            "forward_days":  self.forward_days,
            "ic_window":     self.ic_window,
            "n_factors":     len(reports),
            "valid_factors": valid_n,
            "weights":       weights,
            "factors": [r.to_dict() for r in reports],
            "top5": [
                {"factor": r.factor, "icir": r.icir, "weight": r.weight}
                for r in reports[:5]
            ],
        }

    # ── 快速更新（每日增量計算）──────────────────────────────────────────────

    @classmethod
    def from_updated_df(
        cls,
        new_feat_df: pd.DataFrame,
        factors: list[str] = DEFAULT_FACTORS,
        forward_days: int = 5,
    ) -> dict[str, float]:
        """
        便利方法：直接傳入最新 feat_df，回傳更新後的因子權重字典。
        適合每日排程呼叫。
        """
        engine = cls(new_feat_df, forward_days=forward_days)
        return engine.get_factor_weights(factors)
