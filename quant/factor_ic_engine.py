"""
factor_ic_engine.py — 因子 IC / ICIR 評估引擎

IC (Information Coefficient) = 因子值與未來報酬率的相關係數（Spearman）
ICIR = IC.mean() / IC.std()  —— 越高代表因子越穩定有效

核心功能：
  1. 計算單因子的滾動 IC（每期 vs 下 N 日報酬）
  2. 計算 IC 均值、IC 標準差、ICIR、IC>0 勝率
  3. 批次評估多因子，依 ICIR 排序篩選有效因子
  4. 因子 IC 衰退加權（越新的 IC 佔比越高）
  5. 輸出 FactorReport 可直接供 ensemble_engine 使用

使用方式：
    engine = FactorICEngine(feat_df, forward_days=5)
    report = engine.evaluate_factor("rsi14")
    summary = engine.evaluate_all(DEFAULT_FACTORS)
    top = engine.top_factors(summary, n=5)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)

# 有效因子門檻
MIN_IC_ABS       = 0.02     # |IC 均值| 最小值
MIN_ICIR         = 0.30     # ICIR 最小值（穩定性）
MIN_IC_WIN_RATE  = 0.50     # IC>0 勝率最小值（方向一致性）
MIN_VALID_PERIODS = 20      # 計算 ICIR 所需最少期數

DECAY_ALPHA = 0.95          # IC 時序指數衰退係數（最近 IC 權重較高）

# 預設評估的因子列表（對應 FeatureEngine 欄位名）
DEFAULT_FACTORS = [
    "rsi14",
    "macd_hist",
    "vol_ratio",
    "boll_b",
    "obv_slope5",
    "ma5",
    "ma20",
    "ma60",
    "k", "d",
    "ret_1d", "ret_5d", "ret_10d", "ret_20d",
    "atr14",
    "excess_ret",
    "body_ratio",
    "hl_ratio",
    "boll_width",
]


@dataclass
class FactorReport:
    """單一因子的 IC 分析報告"""
    factor:       str
    n_periods:    int           # 有效計算期數
    ic_mean:      float         # IC 均值（Spearman）
    ic_std:       float         # IC 標準差
    icir:         float         # = ic_mean / ic_std
    ic_win_rate:  float         # IC > 0 的比例
    ic_decay_mean: float        # 衰退加權 IC 均值
    is_valid:     bool          # 是否通過有效門檻
    direction:    int           # +1 / -1（因子正向或負向）
    invalidate_reason: str = ""  # 不通過原因（is_valid=False 時填入）

    def to_dict(self) -> dict:
        return {
            "factor":         self.factor,
            "n_periods":      self.n_periods,
            "ic_mean":        round(self.ic_mean, 4),
            "ic_std":         round(self.ic_std, 4),
            "icir":           round(self.icir, 3),
            "ic_win_rate":    round(self.ic_win_rate, 3),
            "ic_decay_mean":  round(self.ic_decay_mean, 4),
            "is_valid":       self.is_valid,
            "direction":      self.direction,
            "invalidate_reason": self.invalidate_reason,
        }


@dataclass
class EnsembleWeight:
    """供 ensemble_engine 使用的因子加權資訊"""
    factor:    str
    weight:    float    # ICIR 歸一化後的權重（0~1）
    direction: int      # +1 / -1（決定訊號方向）
    icir:      float


class FactorICEngine:
    """
    因子 IC / ICIR 計算引擎。

    feat_df: FeatureEngine.compute_all() 的輸出 DataFrame
             必須包含 close 欄位用於計算前瞻報酬率。
    forward_days: 計算前瞻報酬的天數（預設 5 日）
    rolling_window: 滾動 IC 的視窗長度（預設 60 期）
    """

    def __init__(
        self,
        feat_df: pd.DataFrame,
        forward_days: int = 5,
        rolling_window: int = 60,
    ):
        if "close" not in feat_df.columns:
            raise ValueError("feat_df 必須包含 close 欄位")

        self.df = feat_df.copy().reset_index(drop=True)
        self.forward_days  = forward_days
        self.rolling_window = rolling_window

        # 計算前瞻報酬（shift 避免未來資料洩漏）
        self.df["_fwd_ret"] = (
            self.df["close"].pct_change(forward_days).shift(-forward_days)
        )

    # ── 單因子 IC ─────────────────────────────────────────────────────────

    def _rolling_ic(self, factor_col: str) -> pd.Series:
        """計算逐期（滾動視窗）Spearman IC 序列"""
        ic_vals: list[float] = []
        n = len(self.df)
        window = self.rolling_window

        for i in range(window, n):
            window_df = self.df.iloc[i - window: i]
            x = window_df[factor_col].values
            y = window_df["_fwd_ret"].values
            mask = ~(np.isnan(x) | np.isnan(y))
            if mask.sum() < 10:
                ic_vals.append(np.nan)
                continue
            rho, _ = scipy_stats.spearmanr(x[mask], y[mask])
            ic_vals.append(float(rho) if not np.isnan(rho) else np.nan)

        return pd.Series(ic_vals, dtype=float)

    def _decay_weighted_mean(self, ic_series: pd.Series) -> float:
        """對 IC 序列套用指數衰退加權，越新的 IC 佔比越高"""
        vals = ic_series.dropna().values
        if len(vals) == 0:
            return 0.0
        n = len(vals)
        weights = np.array([DECAY_ALPHA ** (n - 1 - i) for i in range(n)])
        weights /= weights.sum()
        return float(np.dot(weights, vals))

    def evaluate_factor(self, factor: str) -> FactorReport:
        """評估單一因子的 IC 統計量，回傳 FactorReport"""
        if factor not in self.df.columns:
            return FactorReport(
                factor=factor, n_periods=0,
                ic_mean=0.0, ic_std=0.0, icir=0.0,
                ic_win_rate=0.0, ic_decay_mean=0.0,
                is_valid=False, direction=0,
                invalidate_reason=f"欄位 '{factor}' 不存在",
            )

        ic_series = self._rolling_ic(factor)
        valid = ic_series.dropna()
        n = len(valid)

        if n < MIN_VALID_PERIODS:
            return FactorReport(
                factor=factor, n_periods=n,
                ic_mean=0.0, ic_std=0.0, icir=0.0,
                ic_win_rate=0.0, ic_decay_mean=0.0,
                is_valid=False, direction=0,
                invalidate_reason=f"有效期數 {n} < 門檻 {MIN_VALID_PERIODS}",
            )

        ic_mean      = float(valid.mean())
        ic_std       = float(valid.std()) if valid.std() > 1e-8 else 1e-8
        icir         = ic_mean / ic_std
        ic_win_rate  = float((valid > 0).mean())
        ic_decay_mean = self._decay_weighted_mean(valid)
        direction    = 1 if ic_mean >= 0 else -1

        # 有效性判斷
        reasons: list[str] = []
        if abs(ic_mean) < MIN_IC_ABS:
            reasons.append(f"|IC均值|={abs(ic_mean):.4f} < {MIN_IC_ABS}")
        if abs(icir) < MIN_ICIR:
            reasons.append(f"|ICIR|={abs(icir):.3f} < {MIN_ICIR}")
        # 方向一致性：勝率在方向上需 >= 50%
        directional_win = ic_win_rate if ic_mean >= 0 else (1 - ic_win_rate)
        if directional_win < MIN_IC_WIN_RATE:
            reasons.append(f"方向勝率={directional_win*100:.1f}% < {MIN_IC_WIN_RATE*100:.0f}%")

        is_valid = len(reasons) == 0

        return FactorReport(
            factor=factor,
            n_periods=n,
            ic_mean=round(ic_mean, 4),
            ic_std=round(ic_std, 4),
            icir=round(icir, 3),
            ic_win_rate=round(ic_win_rate, 3),
            ic_decay_mean=round(ic_decay_mean, 4),
            is_valid=is_valid,
            direction=direction,
            invalidate_reason="; ".join(reasons),
        )

    # ── 多因子批次評估 ───────────────────────────────────────────────────

    def evaluate_all(self, factors: list[str] = DEFAULT_FACTORS) -> list[FactorReport]:
        """批次評估多因子，依 |ICIR| 排序（有效在前）"""
        reports = []
        for f in factors:
            try:
                r = self.evaluate_factor(f)
            except Exception as e:
                logger.warning(f"[FactorIC] {f} 評估失敗: {e}")
                r = FactorReport(
                    factor=f, n_periods=0,
                    ic_mean=0.0, ic_std=0.0, icir=0.0,
                    ic_win_rate=0.0, ic_decay_mean=0.0,
                    is_valid=False, direction=0,
                    invalidate_reason=str(e),
                )
            reports.append(r)
            logger.debug(
                f"[FactorIC] {f:20s} IC={r.ic_mean:+.4f} "
                f"ICIR={r.icir:+.3f} valid={r.is_valid}"
            )

        # 有效因子優先，再依 |ICIR| 排序
        return sorted(reports, key=lambda r: (not r.is_valid, -abs(r.icir)))

    def top_factors(
        self,
        reports: list[FactorReport],
        n: int = 5,
        valid_only: bool = True,
    ) -> list[FactorReport]:
        """取前 N 個有效因子"""
        filtered = [r for r in reports if r.is_valid] if valid_only else reports
        return filtered[:n]

    # ── 轉換為 ensemble_engine 加權格式 ─────────────────────────────────

    def to_ensemble_weights(
        self,
        reports: list[FactorReport],
        valid_only: bool = True,
    ) -> list[EnsembleWeight]:
        """
        將 FactorReport 列表轉換為 EnsembleWeight，
        以 |ICIR| 歸一化為 0~1 權重。
        """
        valid_reports = [r for r in reports if (r.is_valid or not valid_only) and r.n_periods > 0]
        if not valid_reports:
            return []

        icirs = np.array([abs(r.icir) for r in valid_reports])
        total = icirs.sum()
        if total < 1e-8:
            weights = np.ones(len(icirs)) / len(icirs)
        else:
            weights = icirs / total

        return [
            EnsembleWeight(
                factor=r.factor,
                weight=round(float(w), 4),
                direction=r.direction,
                icir=r.icir,
            )
            for r, w in zip(valid_reports, weights)
        ]

    # ── IC 摘要表 ────────────────────────────────────────────────────────

    def summary_df(self, reports: list[FactorReport]) -> pd.DataFrame:
        """轉換為 DataFrame，方便列印或輸出"""
        rows = [r.to_dict() for r in reports]
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df[[
            "factor", "n_periods", "ic_mean", "ic_std", "icir",
            "ic_win_rate", "ic_decay_mean", "direction", "is_valid",
            "invalidate_reason",
        ]]


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from quant.feature_engine import FeatureEngine, _generate_mock_ohlcv

    mock_df = _generate_mock_ohlcv(500)
    fe = FeatureEngine(mock_df)
    feat_df = fe.compute_all()

    engine = FactorICEngine(feat_df, forward_days=5, rolling_window=60)

    print("=== 因子 IC / ICIR 評估 ===")
    reports = engine.evaluate_all(DEFAULT_FACTORS)
    df = engine.summary_df(reports)
    print(df.to_string(index=False))

    print("\n=== 前 5 有效因子 ===")
    top5 = engine.top_factors(reports, n=5)
    for r in top5:
        print(f"  {r.factor:20s} IC={r.ic_mean:+.4f}  ICIR={r.icir:+.3f}  "
              f"勝率={r.ic_win_rate*100:.1f}%  方向={'多' if r.direction>0 else '空'}")

    print("\n=== Ensemble 加權格式 ===")
    weights = engine.to_ensemble_weights(reports)
    for w in weights[:5]:
        print(f"  {w.factor:20s} weight={w.weight:.4f}  dir={w.direction:+d}  ICIR={w.icir:+.3f}")
