"""
montecarlo_engine.py — 蒙地卡羅回測引擎

功能：
  - 隨機化交易順序模擬 N 次（預設 1000）
  - 輸出最大回撤分布、爆倉機率、勝率穩定性
  - 生成分布圖存成圖片（static/reports/）

使用方式：
    engine = MonteCarloEngine()
    result = engine.run(trades=[0.05, -0.02, 0.08, -0.01, ...])
    img    = engine.generate_chart(result)
    print(result.summary())
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static/reports"))


@dataclass
class MonteCarloResult:
    n_sims:            int
    n_trades:          int
    initial_capital:   float

    # 最大回撤分布
    max_dd_mean:       float          # 平均最大回撤
    max_dd_p50:        float          # 中位數
    max_dd_p95:        float          # 95th percentile（最壞情況）
    max_dd_p99:        float          # 99th percentile

    # 爆倉機率
    bankruptcy_prob:   float          # equity < 0 的模擬比例（0~1）
    ruin_prob_20:      float          # 淨值跌破初始 80% 的機率

    # 最終報酬分布
    final_return_mean: float
    final_return_p5:   float
    final_return_p95:  float

    # 勝率穩定性
    win_rate_mean:     float
    win_rate_std:      float          # 越小越穩定

    # 夏普穩定性
    sharpe_mean:       float
    sharpe_std:        float

    # 樣本曲線（用於繪圖，最多20條）
    sample_curves:     list[list[float]] = field(default_factory=list)

    # 原始陣列（用於繪圖）
    all_max_dds:       list[float] = field(default_factory=list)
    all_final_returns: list[float] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"── 蒙地卡羅模擬（{self.n_sims:,} 次）────────────────",
            f"交易筆數:     {self.n_trades}",
            f"初始資本:     {self.initial_capital:,.0f}",
            "",
            f"最大回撤（平均）: {self.max_dd_mean*100:.1f}%",
            f"最大回撤（P50）:  {self.max_dd_p50*100:.1f}%",
            f"最大回撤（P95）:  {self.max_dd_p95*100:.1f}%",
            f"最大回撤（P99）:  {self.max_dd_p99*100:.1f}%",
            "",
            f"爆倉機率（淨值歸零）: {self.bankruptcy_prob*100:.2f}%",
            f"損失 20% 機率:        {self.ruin_prob_20*100:.1f}%",
            "",
            f"最終報酬（均值）: {self.final_return_mean*100:+.1f}%",
            f"最終報酬（P5）:   {self.final_return_p5*100:+.1f}%",
            f"最終報酬（P95）:  {self.final_return_p95*100:+.1f}%",
            "",
            f"勝率穩定性（σ）: {self.win_rate_std*100:.1f}%",
            f"夏普穩定性（σ）: {self.sharpe_std:.3f}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "n_sims":             self.n_sims,
            "n_trades":           self.n_trades,
            "max_dd_mean_pct":    round(self.max_dd_mean * 100, 2),
            "max_dd_p50_pct":     round(self.max_dd_p50 * 100, 2),
            "max_dd_p95_pct":     round(self.max_dd_p95 * 100, 2),
            "max_dd_p99_pct":     round(self.max_dd_p99 * 100, 2),
            "bankruptcy_prob_pct":round(self.bankruptcy_prob * 100, 3),
            "ruin_prob_20_pct":   round(self.ruin_prob_20 * 100, 2),
            "final_return_mean":  round(self.final_return_mean * 100, 2),
            "final_return_p5":    round(self.final_return_p5 * 100, 2),
            "final_return_p95":   round(self.final_return_p95 * 100, 2),
            "win_rate_mean":      round(self.win_rate_mean * 100, 2),
            "win_rate_std":       round(self.win_rate_std * 100, 2),
            "sharpe_mean":        round(self.sharpe_mean, 3),
            "sharpe_std":         round(self.sharpe_std, 3),
        }


class MonteCarloEngine:
    """
    蒙地卡羅回測引擎。

    接受交易損益列表，隨機化順序 N 次，統計各項風險指標。
    """

    def __init__(
        self,
        n_sims:          int   = 1000,
        initial_capital: float = 1_000_000,
        rng_seed:        Optional[int] = None,
    ):
        self.n_sims   = n_sims
        self.capital  = initial_capital
        self._rng     = np.random.default_rng(rng_seed)

    def run(
        self,
        trades:      list[float],           # 每筆交易損益（元 或 報酬率）
        is_return:   bool = True,           # True=報酬率，False=絕對損益
        n_sims:      Optional[int] = None,
    ) -> MonteCarloResult:
        """
        執行蒙地卡羅模擬。

        trades: 每筆交易的損益 list
                is_return=True  → trades 是報酬率（如 0.05 = +5%）
                is_return=False → trades 是損益金額（如 50000 = +5萬）
        """
        if not trades:
            raise ValueError("trades list is empty")

        trades_arr = np.array(trades, dtype=float)
        n_t        = len(trades_arr)
        n          = n_sims or self.n_sims

        # 轉成報酬率序列
        if not is_return:
            ret_arr = trades_arr / self.capital
        else:
            ret_arr = trades_arr

        max_dds:      list[float] = []
        final_rets:   list[float] = []
        win_rates:    list[float] = []
        sharpes:      list[float] = []
        sample_curves: list[list[float]] = []

        for i in range(n):
            shuffled    = self._rng.permutation(ret_arr)
            equity      = self.capital * np.cumprod(1 + shuffled)
            equity_full = np.concatenate([[self.capital], equity])

            # 最大回撤
            peak  = np.maximum.accumulate(equity_full)
            dd    = (peak - equity_full) / (peak + 1e-9)
            max_dd = float(dd.max())
            max_dds.append(max_dd)

            # 最終報酬
            final_rets.append(float(equity[-1] / self.capital - 1))

            # 勝率
            win_rates.append(float((shuffled > 0).mean()))

            # 夏普（年化簡估）
            std = float(shuffled.std()) if shuffled.std() > 0 else 1e-9
            sharpe = float(shuffled.mean() / std * np.sqrt(252 / max(n_t, 1)))
            sharpes.append(sharpe)

            # 樣本曲線（前20條）
            if i < 20:
                curve = [round(float(equity_full[j] / self.capital - 1) * 100, 2)
                         for j in range(0, len(equity_full), max(1, len(equity_full) // 50))]
                sample_curves.append(curve)

        dd_arr  = np.array(max_dds)
        ret_arr2 = np.array(final_rets)
        wr_arr  = np.array(win_rates)
        sh_arr  = np.array(sharpes)

        return MonteCarloResult(
            n_sims=n,
            n_trades=n_t,
            initial_capital=self.capital,
            max_dd_mean=float(dd_arr.mean()),
            max_dd_p50=float(np.percentile(dd_arr, 50)),
            max_dd_p95=float(np.percentile(dd_arr, 95)),
            max_dd_p99=float(np.percentile(dd_arr, 99)),
            bankruptcy_prob=float((ret_arr2 < -1.0).mean()),  # equity < 0
            ruin_prob_20=float((dd_arr > 0.20).mean()),
            final_return_mean=float(ret_arr2.mean()),
            final_return_p5=float(np.percentile(ret_arr2, 5)),
            final_return_p95=float(np.percentile(ret_arr2, 95)),
            win_rate_mean=float(wr_arr.mean()),
            win_rate_std=float(wr_arr.std()),
            sharpe_mean=float(sh_arr.mean()),
            sharpe_std=float(sh_arr.std()),
            sample_curves=sample_curves,
            all_max_dds=dd_arr.tolist(),
            all_final_returns=ret_arr2.tolist(),
        )

    def generate_chart(
        self,
        result:      MonteCarloResult,
        output_path: Optional[Path] = None,
    ) -> Optional[Path]:
        """
        生成 3 格分布圖（最大回撤直方圖 / 最終報酬直方圖 / 樣本淨值曲線）。
        存入 static/reports/；返回 Path；失敗返回 None。
        """
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches

            STATIC_DIR.mkdir(parents=True, exist_ok=True)
            if output_path is None:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_path = STATIC_DIR / f"mc_{ts}.png"

            BG     = "#0A0F1E"
            PANEL  = "#0D1525"
            WHITE  = "#E8EEF8"
            MUTED  = "#6A7E9C"
            RED    = "#FF4455"
            GREEN  = "#4ADE80"
            BLUE   = "#4A90E2"
            BORDER = "#1C2E48"

            fig, axes = plt.subplots(1, 3, figsize=(15, 5))
            fig.patch.set_facecolor(BG)

            # ── 1. 最大回撤直方圖 ───────────────────────────────────
            ax = axes[0]
            ax.set_facecolor(PANEL)
            ax.hist(np.array(result.all_max_dds) * 100, bins=50,
                    color=RED, alpha=0.8, edgecolor=BORDER, linewidth=0.5)
            ax.axvline(result.max_dd_p50 * 100, color=WHITE, lw=1.5, linestyle="--", label=f"P50: {result.max_dd_p50*100:.1f}%")
            ax.axvline(result.max_dd_p95 * 100, color="#FFAA00", lw=1.5, linestyle=":", label=f"P95: {result.max_dd_p95*100:.1f}%")
            ax.set_title("最大回撤分布", color=WHITE, fontsize=11, pad=8)
            ax.set_xlabel("最大回撤 (%)", color=MUTED, fontsize=9)
            ax.set_ylabel("頻次", color=MUTED, fontsize=9)
            ax.tick_params(colors=MUTED, labelsize=8)
            for spine in ax.spines.values():
                spine.set_color(BORDER)
            ax.legend(fontsize=8, facecolor=PANEL, edgecolor=BORDER, labelcolor=WHITE)

            # ── 2. 最終報酬直方圖 ───────────────────────────────────
            ax = axes[1]
            ax.set_facecolor(PANEL)
            ret_pct = np.array(result.all_final_returns) * 100
            pos_mask = ret_pct >= 0
            ax.hist(ret_pct[pos_mask],  bins=40, color=GREEN, alpha=0.75, edgecolor=BORDER, lw=0.5, label="正報酬")
            ax.hist(ret_pct[~pos_mask], bins=40, color=RED,   alpha=0.75, edgecolor=BORDER, lw=0.5, label="負報酬")
            ax.axvline(result.final_return_mean * 100, color=WHITE, lw=1.5, linestyle="--",
                       label=f"均值: {result.final_return_mean*100:+.1f}%")
            ax.set_title("最終報酬分布", color=WHITE, fontsize=11, pad=8)
            ax.set_xlabel("最終報酬 (%)", color=MUTED, fontsize=9)
            ax.set_ylabel("頻次", color=MUTED, fontsize=9)
            ax.tick_params(colors=MUTED, labelsize=8)
            for spine in ax.spines.values():
                spine.set_color(BORDER)
            ax.legend(fontsize=8, facecolor=PANEL, edgecolor=BORDER, labelcolor=WHITE)

            # ── 3. 樣本淨值曲線 ─────────────────────────────────────
            ax = axes[2]
            ax.set_facecolor(PANEL)
            for curve in result.sample_curves[:20]:
                x = list(range(len(curve)))
                color = GREEN if curve[-1] >= 0 else RED
                ax.plot(x, curve, color=color, alpha=0.25, linewidth=0.8)
            if result.sample_curves:
                mean_curve = [
                    float(np.mean([c[i] for c in result.sample_curves
                                   if i < len(c)]))
                    for i in range(max(len(c) for c in result.sample_curves))
                ]
                ax.plot(range(len(mean_curve)), mean_curve,
                        color=BLUE, lw=2.0, label="平均")
            ax.axhline(0, color=MUTED, lw=0.8, linestyle="--")
            ax.set_title("樣本淨值曲線（前20條）", color=WHITE, fontsize=11, pad=8)
            ax.set_xlabel("交易次序", color=MUTED, fontsize=9)
            ax.set_ylabel("累積報酬 (%)", color=MUTED, fontsize=9)
            ax.tick_params(colors=MUTED, labelsize=8)
            for spine in ax.spines.values():
                spine.set_color(BORDER)
            ax.legend(fontsize=8, facecolor=PANEL, edgecolor=BORDER, labelcolor=WHITE)

            # ── 標題列 ──────────────────────────────────────────────
            fig.suptitle(
                f"蒙地卡羅模擬  N={result.n_sims:,}次  "
                f"爆倉機率={result.bankruptcy_prob*100:.2f}%  "
                f"P95回撤={result.max_dd_p95*100:.1f}%",
                color=WHITE, fontsize=12, fontweight="bold", y=1.01,
            )

            plt.tight_layout(pad=1.5)
            fig.savefig(str(output_path), dpi=130, bbox_inches="tight",
                        facecolor=BG, edgecolor="none")
            plt.close(fig)
            logger.info("[MonteCarlo] Chart saved: %s", output_path)
            return output_path

        except Exception as e:
            logger.error("[MonteCarlo] Chart generation failed: %s", e)
            return None

    def run_from_backtest(self, report) -> MonteCarloResult:
        """
        直接從 BacktestReport 提取交易損益並執行模擬。
        report: BacktestReport（from quant.backtest_engine）
        """
        trades = [t.pnl for t in report.trades if t.pnl is not None]
        if not trades:
            # 若無交易，用等權報酬估算
            if report.equity_curve:
                trades = [
                    ec.get("equity", self.capital) / self.capital - 1.0
                    for ec in report.equity_curve[1:]
                    if ec.get("equity")
                ][:50]
        return self.run(trades, is_return=False)

    def run_mock(self, n_trades: int = 50, win_rate: float = 0.55) -> MonteCarloResult:
        """Mock 資料快速測試"""
        rng    = np.random.default_rng(42)
        wins   = rng.uniform(500, 15000, int(n_trades * win_rate))
        losses = -rng.uniform(200, 8000, n_trades - len(wins))
        trades = list(np.concatenate([wins, losses]))
        return self.run(trades, is_return=False)


_global_mc: Optional[MonteCarloEngine] = None


def get_montecarlo_engine() -> MonteCarloEngine:
    global _global_mc
    if _global_mc is None:
        _global_mc = MonteCarloEngine()
    return _global_mc


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== 蒙地卡羅引擎測試 ===")
    engine = MonteCarloEngine(n_sims=500, initial_capital=1_000_000)
    result = engine.run_mock(n_trades=60, win_rate=0.58)
    print(result.summary())

    path = engine.generate_chart(result)
    if path:
        print(f"\n圖片已儲存：{path}")
    else:
        print("\n（matplotlib 不可用，跳過圖片）")
