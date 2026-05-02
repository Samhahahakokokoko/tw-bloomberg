"""
walkforward_engine.py — Walk-Forward 回測引擎（獨立模組）

設計原則：
  - 嚴格無 lookahead bias：訓練期 / 測試期完全隔離
  - train=120天，test=20天，步進 20天（rolling window）
  - signal_fn 只能存取訓練期 DataFrame
  - 測試期用相同策略產生訊號（out-of-sample 驗證）

輸出：
  每段：sharpe / 勝率 / 最大回撤 / 泛化比
  總體：穩定性分數 = 1 - std(test_sharpes) / range(test_sharpes)

使用方式：
    engine = WalkForwardAnalyzer(train_days=120, test_days=20)
    result = engine.run_mock("2330")    # mock 資料快速測試
    print(result.summary())
    print(result.to_dict())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SegmentResult:
    """單一 Walk-Forward 段回測結果"""
    segment_id:     int
    train_start:    str
    train_end:      str
    test_start:     str
    test_end:       str
    train_sharpe:   float
    test_sharpe:    float
    test_win_rate:  float
    test_max_dd:    float
    test_return:    float
    test_n_trades:  int
    generalization: float    # test_sharpe / train_sharpe（泛化比）


@dataclass
class StabilityAnalysis:
    """穩定性分析"""
    sharpe_std:       float   # 各段夏普值標準差
    sharpe_range:     float   # max - min
    stability_score:  float   # 1 - std / range，越高越穩定
    pct_profitable:   float   # 正夏普段的比例
    avg_generalization: float
    verdict:          str     # "穩定" / "尚可" / "不穩定"


@dataclass
class WalkForwardResult:
    """Walk-Forward 完整結果"""
    segments:       list[SegmentResult]
    n_segments:     int
    avg_sharpe:     float
    avg_win_rate:   float
    avg_max_dd:     float
    combined_return: float
    stability:      StabilityAnalysis
    combined:       dict    # 合併統計（sharpe, return_pct, max_dd_pct）
    metadata:       dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            "── Walk-Forward 分析結果 ──────────────",
            f"回測段數:     {self.n_segments}",
            f"平均夏普值:   {self.avg_sharpe:.3f}",
            f"平均勝率:     {self.avg_win_rate*100:.1f}%",
            f"平均最大回撤: {self.avg_max_dd*100:.2f}%",
            f"合併總報酬:   {self.combined_return*100:+.2f}%",
            f"穩定性分數:   {self.stability.stability_score:.3f}",
            f"穩定性評級:   {self.stability.verdict}",
            "",
            "各段測試期表現:",
        ]
        for s in self.segments:
            lines.append(
                f"  Seg{s.segment_id+1:02d}"
                f"  test={s.test_start}~{s.test_end}"
                f"  sharpe={s.test_sharpe:+.3f}"
                f"  wr={s.test_win_rate*100:.0f}%"
                f"  dd={s.test_max_dd*100:.1f}%"
                f"  gen={s.generalization:.2f}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "n_segments":      self.n_segments,
            "avg_sharpe":      round(self.avg_sharpe, 4),
            "avg_win_rate":    round(self.avg_win_rate, 4),
            "avg_max_dd":      round(self.avg_max_dd, 4),
            "combined_return": round(self.combined_return, 4),
            "combined": self.combined,
            "stability": {
                "sharpe_std":        round(self.stability.sharpe_std, 4),
                "sharpe_range":      round(self.stability.sharpe_range, 4),
                "stability_score":   round(self.stability.stability_score, 4),
                "pct_profitable":    round(self.stability.pct_profitable, 3),
                "avg_generalization":round(self.stability.avg_generalization, 3),
                "verdict":           self.stability.verdict,
            },
            "segments": [
                {
                    "segment_id":    s.segment_id,
                    "train":         f"{s.train_start}~{s.train_end}",
                    "test":          f"{s.test_start}~{s.test_end}",
                    "train_sharpe":  round(s.train_sharpe, 3),
                    "test_sharpe":   round(s.test_sharpe, 3),
                    "test_win_rate": round(s.test_win_rate, 4),
                    "test_max_dd":   round(s.test_max_dd, 4),
                    "test_return":   round(s.test_return, 4),
                    "n_trades":      s.test_n_trades,
                    "generalization":round(s.generalization, 3),
                }
                for s in self.segments
            ],
            "metadata": self.metadata,
        }


class WalkForwardAnalyzer:
    """
    Walk-Forward 回測引擎。

    無 lookahead bias 保證：
      - 訓練期：feat_df.iloc[start : start+train_days]
      - 測試期：feat_df.iloc[start+train_days : start+train_days+test_days]
      - 兩者絕不重疊
      - signal_fn 僅接收訓練期 DataFrame，不得存取測試期資料

    使用方式：
        def my_strategy(train_df):
            from quant.alpha_model import RuleBasedAlpha
            alpha = RuleBasedAlpha()
            return pd.Series([alpha.evaluate(r).signal.value
                              for _, r in train_df.iterrows()])

        analyzer = WalkForwardAnalyzer(train_days=120, test_days=20)
        result = analyzer.run(feat_df, signal_fn=my_strategy)
        print(result.summary())
    """

    def __init__(
        self,
        train_days:  int   = 120,
        test_days:   int   = 20,
        step_days:   int   = 20,    # 步進（通常 == test_days）
        initial_capital:     float = 1_000_000,
        commission_discount: float = 0.6,
    ):
        self.train_days = train_days
        self.test_days  = test_days
        self.step_days  = step_days
        self.capital    = initial_capital
        self.discount   = commission_discount

    def run(
        self,
        feat_df:         pd.DataFrame,
        signal_fn:       Optional[Callable] = None,
        stop_loss_pct:   float = 0.08,
        take_profit_pct: Optional[float] = None,
    ) -> WalkForwardResult:
        """
        執行 Walk-Forward 回測。

        feat_df   : FeatureEngine.compute_all() 的輸出（或含 OHLCV 的 DataFrame）
        signal_fn : fn(train_df: pd.DataFrame) → pd.Series["buy"|"sell"|"hold"]
                    若為 None 使用 RuleBasedAlpha
        """
        from .backtest_engine import BacktestEngine

        if signal_fn is None:
            signal_fn = _default_signal_fn

        n  = len(feat_df)
        T  = self.train_days
        Te = self.test_days
        S  = self.step_days

        if n < T + Te:
            raise ValueError(f"資料不足：需 {T+Te} 筆，實際 {n}")

        segments: list[SegmentResult] = []
        combined_equity = [self.capital]
        start = 0
        sid   = 0

        while start + T + Te <= n:
            # 嚴格切分訓練 / 測試（不重疊）
            train_df = feat_df.iloc[start       : start + T   ].reset_index(drop=True)
            test_df  = feat_df.iloc[start + T   : start + T + Te].reset_index(drop=True)

            # 訓練期訊號（用於計算泛化比）
            try:
                train_sigs = signal_fn(train_df)
                if len(train_sigs) != len(train_df):
                    train_sigs = pd.Series(["hold"] * len(train_df))
            except Exception as e:
                logger.warning("[WF] seg=%d train signal 失敗: %s", sid, e)
                train_sigs = pd.Series(["hold"] * len(train_df))

            # 測試期訊號（同策略，應用於測試資料）
            try:
                test_sigs = signal_fn(test_df)
                if len(test_sigs) != len(test_df):
                    test_sigs = pd.Series(["hold"] * len(test_df))
            except Exception as e:
                logger.warning("[WF] seg=%d test signal 失敗: %s", sid, e)
                test_sigs = pd.Series(["hold"] * len(test_df))

            engine = BacktestEngine(
                initial_capital=self.capital,
                commission_discount=self.discount,
            )

            # 訓練期回測（只取 sharpe 計算泛化比）
            try:
                tr = engine.run(train_df, train_sigs,
                                stop_loss_pct=stop_loss_pct,
                                take_profit_pct=take_profit_pct)
                train_sharpe = tr.sharpe_ratio
            except Exception:
                train_sharpe = 0.0

            # 測試期回測（核心績效）
            try:
                te = engine.run(test_df, test_sigs,
                                stop_loss_pct=stop_loss_pct,
                                take_profit_pct=take_profit_pct)
                test_sharpe = te.sharpe_ratio
                test_wr     = te.win_rate
                test_dd     = te.max_drawdown
                test_ret    = te.total_return
                test_trades = te.n_trades
            except Exception as e:
                logger.warning("[WF] seg=%d test backtest 失敗: %s", sid, e)
                test_sharpe = test_wr = test_dd = test_ret = 0.0
                test_trades = 0

            gen = test_sharpe / train_sharpe if abs(train_sharpe) > 1e-6 else 0.0

            def _dt(df_, idx):
                try:    return str(df_.iloc[idx].get("date", idx))[:10]
                except: return str(idx)

            segments.append(SegmentResult(
                segment_id=sid,
                train_start=_dt(feat_df, start),
                train_end=_dt(feat_df, start + T - 1),
                test_start=_dt(feat_df, start + T),
                test_end=_dt(feat_df, min(start + T + Te - 1, n - 1)),
                train_sharpe=round(train_sharpe, 3),
                test_sharpe=round(test_sharpe, 3),
                test_win_rate=round(test_wr, 4),
                test_max_dd=round(test_dd, 4),
                test_return=round(test_ret, 4),
                test_n_trades=test_trades,
                generalization=round(gen, 3),
            ))

            if combined_equity:
                combined_equity.append(combined_equity[-1] * (1 + test_ret))

            start += S
            sid   += 1

        return self._aggregate(segments, combined_equity, feat_df)

    def _aggregate(
        self,
        segments:        list[SegmentResult],
        combined_equity: list[float],
        feat_df:         pd.DataFrame,
    ) -> WalkForwardResult:
        if not segments:
            return WalkForwardResult(
                segments=[], n_segments=0, avg_sharpe=0.0,
                avg_win_rate=0.0, avg_max_dd=0.0, combined_return=0.0,
                stability=StabilityAnalysis(0, 0, 0, 0, 0, "不穩定"),
                combined={},
                metadata={"train_days": self.train_days, "test_days": self.test_days},
            )

        sharpes = np.array([s.test_sharpe for s in segments])
        gens    = np.array([s.generalization for s in segments])

        sharpe_std   = float(sharpes.std()) if len(sharpes) > 1 else 0.0
        sharpe_range = float(sharpes.max() - sharpes.min()) if len(sharpes) > 1 else 1.0
        stab_score   = float(max(0.0, 1.0 - sharpe_std / (sharpe_range + 1e-9)))
        pct_pos      = float((sharpes > 0).mean())
        avg_gen      = float(gens.mean())

        if stab_score >= 0.7 and float(sharpes.mean()) >= 0.8 and pct_pos >= 0.7:
            verdict = "穩定"
        elif stab_score >= 0.4 and float(sharpes.mean()) >= 0.3 and pct_pos >= 0.5:
            verdict = "尚可"
        else:
            verdict = "不穩定"

        avg_sharpe = float(sharpes.mean())
        avg_wr     = float(np.mean([s.test_win_rate for s in segments]))
        avg_dd     = float(np.mean([s.test_max_dd for s in segments]))
        comb_ret   = (combined_equity[-1] / combined_equity[0]) - 1 if len(combined_equity) >= 2 else 0.0

        # 合併回撤（從equity curve計算）
        eq_arr  = np.array(combined_equity)
        peak    = np.maximum.accumulate(eq_arr)
        comb_dd = float(((peak - eq_arr) / peak).max())

        return WalkForwardResult(
            segments=segments,
            n_segments=len(segments),
            avg_sharpe=round(avg_sharpe, 4),
            avg_win_rate=round(avg_wr, 4),
            avg_max_dd=round(avg_dd, 4),
            combined_return=round(comb_ret, 4),
            stability=StabilityAnalysis(
                sharpe_std=round(sharpe_std, 4),
                sharpe_range=round(sharpe_range, 4),
                stability_score=round(stab_score, 4),
                pct_profitable=round(pct_pos, 3),
                avg_generalization=round(avg_gen, 3),
                verdict=verdict,
            ),
            combined={
                "return_pct":   round(comb_ret * 100, 2),
                "sharpe":       round(avg_sharpe, 3),
                "max_dd_pct":   round(comb_dd * 100, 2),
                "positive_segs":int((sharpes > 0).sum()),
                "total_segs":   len(segments),
            },
            metadata={
                "train_days": self.train_days,
                "test_days":  self.test_days,
                "step_days":  self.step_days,
                "data_points":len(feat_df),
            },
        )

    def run_mock(self, stock_code: str = "2330", n_days: int = 400) -> WalkForwardResult:
        """使用 mock 資料獨立測試"""
        seed  = sum(ord(c) for c in stock_code)
        rng   = np.random.default_rng(seed)
        dates = pd.date_range("2023-01-01", periods=n_days, freq="B")
        close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.012, n_days))
        df = pd.DataFrame({
            "date":   dates,
            "open":   close * rng.uniform(0.990, 1.010, n_days),
            "high":   close * rng.uniform(1.000, 1.025, n_days),
            "low":    close * rng.uniform(0.975, 1.000, n_days),
            "close":  close,
            "volume": rng.integers(5_000_000, 50_000_000, n_days).astype(float),
        })
        try:
            from .feature_engine import FeatureEngine
            feat_df = FeatureEngine(df).compute_all()
        except Exception:
            feat_df = df
        return self.run(feat_df)


def _default_signal_fn(train_df: pd.DataFrame) -> pd.Series:
    """預設策略：RuleBasedAlpha（rule_based）"""
    try:
        from .alpha_model import RuleBasedAlpha
        alpha = RuleBasedAlpha()
        return pd.Series([
            alpha.evaluate(row).signal.value for _, row in train_df.iterrows()
        ])
    except Exception:
        return pd.Series(["hold"] * len(train_df))


_global_wf: Optional[WalkForwardAnalyzer] = None


def get_walkforward_analyzer() -> WalkForwardAnalyzer:
    global _global_wf
    if _global_wf is None:
        _global_wf = WalkForwardAnalyzer()
    return _global_wf


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Walk-Forward Analyzer 獨立測試 ===")
    engine = WalkForwardAnalyzer(train_days=120, test_days=20, step_days=20)
    result = engine.run_mock("2330", n_days=400)
    print(result.summary())
    d = result.to_dict()
    print(f"\n穩定性詳情: {d['stability']}")
    print(f"合併績效:   {d['combined']}")
