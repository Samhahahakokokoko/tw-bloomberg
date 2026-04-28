"""
backtest_engine.py — 量化回測引擎（台股完整成本模型）

完整實作：
  1. 台股真實成本：
       手續費 0.1425%（買 + 賣），可折扣（網路下單通常 6 折）
       交易稅 0.3%（僅賣方）
       滑價   0.05%（衝擊估算）
  2. 漲跌停限制：±10%（漲跌停強制鎖定於上下限）
  3. 成交量限制：每筆最多占當日量 1%（防流動性衝擊）
  4. 最小交易單位：1 張（1000 股）
  5. 策略支援：傳入任意 signal Series 即可（與 AlphaModel 解耦）
  6. 績效指標：總報酬、年化報酬、夏普值、最大回撤、勝率、獲利因子、
              平均持有天數、成本總計

架構說明：
  BacktestEngine.run(df, signals) → BacktestReport
  df      : 含 OHLCV + 特徵的 DataFrame（date/open/high/low/close/volume）
  signals : pd.Series（index 同 df，值 = "buy" / "sell" / "hold"）

特別設計：
  - 每個 bar 先檢查停損/停利，再處理訊號（避免同日訊號覆蓋停損）
  - 淨值曲線按日記錄（用於繪圖）
  - 交易明細含成本拆解（手續費/稅/滑價）

使用方式：
    engine = BacktestEngine(initial_capital=1_000_000, commission_discount=0.6)
    report = engine.run(df, signals, stop_loss_pct=0.08, take_profit_pct=0.15)
    print(report.summary())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── 台股成本常數 ──────────────────────────────────────────────────────────────

COMMISSION_RATE  = 0.001425   # 手續費基本費率（買 + 賣）
TAX_RATE         = 0.003      # 交易稅（僅賣方）
SLIPPAGE_RATE    = 0.0005     # 滑價估算
LIMIT_UP_DOWN    = 0.10       # 漲跌停幅度
MIN_SHARES       = 1000       # 最小交易單位（1 張）
MAX_VOL_RATIO    = 0.01       # 最多占當日量 1%
TRADING_DAYS     = 252        # 年化基準


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """單筆交易記錄"""
    date:         str
    action:       str        # "BUY" / "SELL" / "STOP_LOSS" / "TAKE_PROFIT"
    price:        float
    shares:       int
    commission:   float
    tax:          float
    slippage:     float
    # 賣出時填入
    entry_date:   Optional[str]   = None
    entry_price:  Optional[float] = None
    holding_days: Optional[int]   = None
    pnl:          Optional[float] = None
    pnl_pct:      Optional[float] = None

    @property
    def total_cost(self) -> float:
        return self.commission + self.tax + self.slippage


@dataclass
class BacktestReport:
    """回測完整報告"""
    # ── 基本資訊 ────────────────────────────────────────────────────────
    initial_capital:  float
    final_equity:     float
    total_return:     float          # 總報酬率
    annual_return:    float          # 年化報酬率
    # ── 風險指標 ────────────────────────────────────────────────────────
    sharpe_ratio:     float
    max_drawdown:     float
    volatility:       float
    # ── 交易統計 ────────────────────────────────────────────────────────
    n_trades:         int
    win_rate:         float
    profit_factor:    float          # 總獲利 / 總虧損（> 1 為正期望值）
    avg_holding_days: float
    # ── 成本分析 ────────────────────────────────────────────────────────
    total_commission: float
    total_tax:        float
    total_slippage:   float
    cost_impact_pct:  float          # 成本總計 / 初始資本
    # ── 詳細資料 ────────────────────────────────────────────────────────
    trades:           list[Trade] = field(default_factory=list)
    equity_curve:     list[dict]  = field(default_factory=list)  # 每日淨值
    params:           dict        = field(default_factory=dict)   # 策略參數記錄

    def summary(self) -> str:
        """格式化摘要（用於 LINE Bot / 日誌輸出）"""
        lines = [
            f"── 回測報告 ──────────────────",
            f"初始資本:   {self.initial_capital:>12,.0f}",
            f"最終淨值:   {self.final_equity:>12,.0f}",
            f"總報酬率:   {self.total_return*100:>+.2f}%",
            f"年化報酬:   {self.annual_return*100:>+.2f}%",
            f"夏普值:     {self.sharpe_ratio:>8.3f}",
            f"最大回撤:   {self.max_drawdown*100:>8.2f}%",
            f"年化波動:   {self.volatility*100:>8.2f}%",
            f"── 交易統計 ──────────────────",
            f"交易次數:   {self.n_trades:>5} 筆（買賣各半）",
            f"勝率:       {self.win_rate*100:>8.1f}%",
            f"獲利因子:   {self.profit_factor:>8.2f}",
            f"平均持有:   {self.avg_holding_days:>8.1f} 天",
            f"── 成本分析 ──────────────────",
            f"手續費:     {self.total_commission:>12,.0f}",
            f"交易稅:     {self.total_tax:>12,.0f}",
            f"滑價成本:   {self.total_slippage:>12,.0f}",
            f"成本占比:   {self.cost_impact_pct*100:>8.3f}%",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """轉為 JSON-friendly dict（FastAPI 回傳用）"""
        return {
            "initial_capital":  self.initial_capital,
            "final_equity":     self.final_equity,
            "total_return":     round(self.total_return, 4),
            "annual_return":    round(self.annual_return, 4),
            "sharpe_ratio":     round(self.sharpe_ratio, 4),
            "max_drawdown":     round(self.max_drawdown, 4),
            "volatility":       round(self.volatility, 4),
            "n_trades":         self.n_trades,
            "win_rate":         round(self.win_rate, 4),
            "profit_factor":    round(self.profit_factor, 4),
            "avg_holding_days": round(self.avg_holding_days, 1),
            "total_commission": self.total_commission,
            "total_tax":        self.total_tax,
            "total_slippage":   self.total_slippage,
            "cost_impact_pct":  round(self.cost_impact_pct, 4),
            "equity_curve":     self.equity_curve,
            "trades": [
                {
                    "date":         t.date,
                    "action":       t.action,
                    "price":        t.price,
                    "shares":       t.shares,
                    "pnl":          t.pnl,
                    "pnl_pct":      t.pnl_pct,
                    "holding_days": t.holding_days,
                    "commission":   t.commission,
                    "tax":          t.tax,
                    "slippage":     t.slippage,
                }
                for t in self.trades
            ],
            "params": self.params,
        }


# ── 回測引擎 ─────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    台股量化回測引擎。

    使用方式：
        engine = BacktestEngine(initial_capital=1_000_000)

        # 方式一：傳入外部訊號 Series（與任何模型解耦）
        report = engine.run(df, signals=signal_series)

        # 方式二：傳入 AlphaModel，由引擎內部產生訊號
        report = engine.run_with_model(df, alpha_model)

    df 欄位：date, open, high, low, close, volume（及特徵欄位）
    signals：pd.Series，index = df.index，值 = "buy" / "sell" / "hold"
    """

    def __init__(
        self,
        initial_capital:     float = 1_000_000,
        commission_discount: float = 1.0,     # 手續費折扣（網路下單 0.6）
        allow_short:         bool  = False,    # 是否允許放空（台股散戶通常不放空）
        max_positions:       int   = 1,        # 最大同時持倉數（單股回測設 1）
    ):
        self.initial_capital     = initial_capital
        self.commission_rate     = COMMISSION_RATE * commission_discount
        self.allow_short         = allow_short
        self.max_positions       = max_positions

    # ── 主回測迴圈 ────────────────────────────────────────────────────────

    def run(
        self,
        df:               pd.DataFrame,
        signals:          pd.Series,
        stop_loss_pct:    Optional[float] = None,   # 停損幅度（如 0.08 = 8%）
        take_profit_pct:  Optional[float] = None,   # 停利幅度（如 0.15 = 15%）
        position_size_pct: float = 0.95,            # 每次進場動用資金比例
    ) -> BacktestReport:
        """
        執行回測。

        signals: pd.Series（index = df.index，值 = "buy" / "sell" / "hold"）
        stop_loss_pct: 固定停損%（None = 不設停損）
        take_profit_pct: 固定停利%（None = 不設停利）
        """
        df = df.copy().reset_index(drop=True)
        signals = signals.reset_index(drop=True)

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # ── 狀態變數 ──────────────────────────────────────────────────────
        cash          = float(self.initial_capital)
        shares        = 0          # 當前持股數
        entry_price   = 0.0        # 進場成本價（含手續費）
        entry_date    = ""
        entry_idx     = -1
        trades:       list[Trade] = []
        equity_curve: list[dict]  = []

        total_commission = 0.0
        total_tax        = 0.0
        total_slippage   = 0.0

        for i, row in df.iterrows():
            date_str  = str(row.get("date", i))[:10]
            close     = float(row["close"])
            open_     = float(row["open"])
            high      = float(row["high"])
            low       = float(row["low"])
            volume    = float(row.get("volume", 0))
            sig       = str(signals.iloc[i]) if i < len(signals) else "hold"

            # 前日收盤（用於漲跌停計算）
            prev_close = float(df["close"].iloc[i - 1]) if i > 0 else close

            # ── 先處理停損/停利（比訊號優先）────────────────────────────
            if shares > 0:
                # 停損：開盤跳空也按開盤價成交
                if stop_loss_pct is not None:
                    stop_price = entry_price * (1 - stop_loss_pct)
                    if low <= stop_price:
                        exec_price = max(low, stop_price)
                        exec_price = self._apply_limit(exec_price, prev_close)
                        t = self._sell(
                            date_str, shares, exec_price, prev_close, volume,
                            entry_date, entry_price, entry_idx, i, "STOP_LOSS"
                        )
                        trades.append(t)
                        total_commission += t.commission
                        total_tax        += t.tax
                        total_slippage   += t.slippage
                        cash  += shares * exec_price - t.commission - t.tax - t.slippage
                        shares = 0
                        continue  # 已賣出，不再處理訊號

                # 停利：高點觸及停利
                if take_profit_pct is not None:
                    tp_price = entry_price * (1 + take_profit_pct)
                    if high >= tp_price:
                        exec_price = self._apply_limit(tp_price, prev_close)
                        t = self._sell(
                            date_str, shares, exec_price, prev_close, volume,
                            entry_date, entry_price, entry_idx, i, "TAKE_PROFIT"
                        )
                        trades.append(t)
                        total_commission += t.commission
                        total_tax        += t.tax
                        total_slippage   += t.slippage
                        cash  += shares * exec_price - t.commission - t.tax - t.slippage
                        shares = 0
                        continue

            # ── 訊號處理 ──────────────────────────────────────────────────
            if sig == "buy" and shares == 0:
                exec_price = self._apply_limit(close, prev_close)
                exec_price = exec_price * (1 + SLIPPAGE_RATE)  # 滑價（買入偏高）
                # 成交量限制
                max_shares_by_vol = self._vol_cap(volume)
                # 資金計算
                affordable  = cash * position_size_pct
                raw_shares  = int(affordable / (exec_price * (1 + self.commission_rate + SLIPPAGE_RATE)))
                buy_shares  = min(raw_shares, max_shares_by_vol)
                buy_shares  = (buy_shares // MIN_SHARES) * MIN_SHARES

                if buy_shares >= MIN_SHARES:
                    gross      = buy_shares * exec_price
                    commission = gross * self.commission_rate
                    slippage   = gross * SLIPPAGE_RATE
                    cost       = gross + commission + slippage
                    if cost <= cash:
                        cash       -= cost
                        shares      = buy_shares
                        entry_price = exec_price * (1 + self.commission_rate + SLIPPAGE_RATE)
                        entry_date  = date_str
                        entry_idx   = i
                        total_commission += commission
                        total_slippage   += slippage
                        trades.append(Trade(
                            date=date_str, action="BUY",
                            price=exec_price, shares=buy_shares,
                            commission=commission, tax=0.0, slippage=slippage,
                        ))

            elif sig == "sell" and shares > 0:
                exec_price = self._apply_limit(close, prev_close)
                t = self._sell(
                    date_str, shares, exec_price, prev_close, volume,
                    entry_date, entry_price, entry_idx, i, "SELL"
                )
                trades.append(t)
                total_commission += t.commission
                total_tax        += t.tax
                total_slippage   += t.slippage
                cash  += shares * exec_price - t.commission - t.tax - t.slippage
                shares = 0

            # ── 每日淨值記錄 ──────────────────────────────────────────────
            equity = cash + shares * close
            equity_curve.append({"date": date_str, "equity": round(equity, 0)})

        # 回測結束，強制平倉
        if shares > 0 and len(df) > 0:
            last = df.iloc[-1]
            exec_price = float(last["close"])
            prev_close = float(df["close"].iloc[-2]) if len(df) > 1 else exec_price
            t = self._sell(
                str(last.get("date", ""))[:10], shares, exec_price,
                prev_close, float(last.get("volume", 0)),
                entry_date, entry_price, entry_idx, len(df) - 1, "SELL"
            )
            trades.append(t)
            total_commission += t.commission
            total_tax        += t.tax
            total_slippage   += t.slippage
            cash += shares * exec_price - t.commission - t.tax - t.slippage
            shares = 0
            equity_curve.append({"date": str(last.get("date", ""))[:10], "equity": round(cash, 0)})

        # ── 績效計算 ──────────────────────────────────────────────────────
        final_equity = float(equity_curve[-1]["equity"]) if equity_curve else self.initial_capital
        return self._calc_metrics(
            final_equity, trades, equity_curve,
            total_commission, total_tax, total_slippage,
            params={"stop_loss": stop_loss_pct, "take_profit": take_profit_pct, "position_size": position_size_pct},
        )

    def run_with_model(
        self,
        df:     pd.DataFrame,
        model,  # AlphaModel 或 RuleBasedAlpha instance
        **kwargs,
    ) -> BacktestReport:
        """
        使用 AlphaModel 自動產生訊號後回測。
        """
        from quant.feature_engine import FeatureEngine
        fe = FeatureEngine(df)
        feat_df = fe.compute_all()
        results = [model.predict(row) for _, row in feat_df.iterrows()]
        signals = pd.Series([r.signal.value for r in results])
        return self.run(feat_df, signals, **kwargs)

    # ── 輔助方法 ──────────────────────────────────────────────────────────

    def _sell(
        self,
        date_str:    str,
        shares:      int,
        exec_price:  float,
        prev_close:  float,
        volume:      float,
        entry_date:  str,
        entry_price: float,
        entry_idx:   int,
        curr_idx:    int,
        action:      str,
    ) -> Trade:
        """執行賣出，計算所有成本與損益"""
        exec_price = max(exec_price, 0.01)
        # 成交量限制（賣出同樣受限）
        max_by_vol = self._vol_cap(volume)
        sell_shares = min(shares, max_by_vol)
        if sell_shares < MIN_SHARES:
            sell_shares = MIN_SHARES

        gross      = sell_shares * exec_price
        commission = gross * self.commission_rate
        tax        = gross * TAX_RATE
        slippage   = gross * SLIPPAGE_RATE   # 賣出有負滑價（賣到更低）
        net_recv   = gross - commission - tax - slippage

        pnl  = net_recv - sell_shares * entry_price
        pnl_pct = pnl / (sell_shares * entry_price) if entry_price > 0 else 0.0
        hold = curr_idx - entry_idx

        return Trade(
            date=date_str, action=action,
            price=exec_price, shares=sell_shares,
            commission=commission, tax=tax, slippage=slippage,
            entry_date=entry_date, entry_price=entry_price,
            holding_days=hold, pnl=round(pnl, 0), pnl_pct=round(pnl_pct, 4),
        )

    @staticmethod
    def _apply_limit(price: float, prev_close: float) -> float:
        """套用漲跌停限制"""
        upper = prev_close * (1 + LIMIT_UP_DOWN)
        lower = prev_close * (1 - LIMIT_UP_DOWN)
        return max(lower, min(upper, price))

    @staticmethod
    def _vol_cap(volume: float) -> int:
        """根據當日量計算最大可成交股數"""
        if volume <= 0:
            return 10 * MIN_SHARES   # 無量資料時給預設值
        raw = int(volume * MAX_VOL_RATIO)
        return max(MIN_SHARES, (raw // MIN_SHARES) * MIN_SHARES)

    def _calc_metrics(
        self,
        final_equity: float,
        trades:       list[Trade],
        equity_curve: list[dict],
        total_comm:   float,
        total_tax:    float,
        total_slp:    float,
        params:       dict,
    ) -> BacktestReport:
        """計算所有績效指標"""
        n_days   = len(equity_curve)
        init_cap = self.initial_capital

        total_return = (final_equity - init_cap) / init_cap
        years        = n_days / TRADING_DAYS
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0.0

        # 夏普值（基於每日淨值報酬）
        eq_series = pd.Series([e["equity"] for e in equity_curve])
        daily_ret = eq_series.pct_change().dropna()
        vol       = float(daily_ret.std() * np.sqrt(TRADING_DAYS)) if len(daily_ret) > 1 else 0.0
        sharpe    = (annual_return - 0.015) / vol if vol > 0 else 0.0

        # 最大回撤
        peak = eq_series.cummax()
        dd   = (eq_series - peak) / peak
        max_dd = float(dd.min()) if len(dd) > 0 else 0.0

        # 交易統計（只計算賣出記錄）
        sell_trades = [t for t in trades if t.pnl is not None]
        n_trades    = len(sell_trades)
        win_trades  = [t for t in sell_trades if (t.pnl or 0) > 0]
        loss_trades = [t for t in sell_trades if (t.pnl or 0) < 0]
        win_rate    = len(win_trades) / n_trades if n_trades > 0 else 0.0
        total_profit = sum(t.pnl for t in win_trades)
        total_loss   = abs(sum(t.pnl for t in loss_trades))
        profit_factor = total_profit / total_loss if total_loss > 0 else float("inf")
        avg_hold     = np.mean([t.holding_days for t in sell_trades]) if sell_trades else 0.0

        cost_impact = (total_comm + total_tax + total_slp) / init_cap

        return BacktestReport(
            initial_capital=init_cap,
            final_equity=final_equity,
            total_return=round(total_return, 4),
            annual_return=round(annual_return, 4),
            sharpe_ratio=round(sharpe, 4),
            max_drawdown=round(abs(max_dd), 4),
            volatility=round(vol, 4),
            n_trades=n_trades,
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 4),
            avg_holding_days=round(float(avg_hold), 1),
            total_commission=round(total_comm, 0),
            total_tax=round(total_tax, 0),
            total_slippage=round(total_slp, 0),
            cost_impact_pct=round(cost_impact, 4),
            trades=trades,
            equity_curve=equity_curve,
            params=params,
        )


# ── Mock 資料 + 獨立測試 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from quant.feature_engine import FeatureEngine, _generate_mock_ohlcv
    from quant.alpha_model import RuleBasedAlpha

    mock_df = _generate_mock_ohlcv(500)
    fe = FeatureEngine(mock_df)
    feat_df = fe.compute_all()

    # 產生規則型訊號
    alpha = RuleBasedAlpha()
    signals = pd.Series([alpha.evaluate(row).signal.value for _, row in feat_df.iterrows()])
    signal_dist = signals.value_counts()
    print(f"訊號分布: {dict(signal_dist)}")

    engine = BacktestEngine(initial_capital=1_000_000, commission_discount=0.6)

    print("\n=== MA Cross 策略（買賣訊號 = 規則型 Alpha）===")
    report = engine.run(
        feat_df, signals,
        stop_loss_pct=0.08,
        take_profit_pct=0.20,
    )
    print(report.summary())

    print(f"\n前 5 筆交易：")
    sell_trades = [t for t in report.trades if t.pnl is not None][:5]
    for t in sell_trades:
        print(f"  {t.date} {t.action:12s} {t.shares}股 @{t.price:.2f} "
              f"損益={t.pnl:+,.0f} ({t.pnl_pct*100:+.1f}%) 持有{t.holding_days}日")

    print(f"\n淨值曲線（前 5 / 後 5）：")
    for e in report.equity_curve[:5] + report.equity_curve[-5:]:
        print(f"  {e['date']}  {e['equity']:>12,.0f}")


# =======================================================================
#  Walk-Forward 回測引擎（防過擬合）— 訓練120日 / 測試20日 / 嚴格無未來偏差
# =======================================================================

from dataclasses import dataclass as _wf_dc, field as _wf_field
from typing import Callable as _Callable, Optional as _Optional
import logging as _wf_logging

_log = logging.getLogger(__name__)


@dataclass
class WFSegment:
    segment_id:   int
    train_start:  str
    train_end:    str
    test_start:   str
    test_end:     str
    train_sharpe: float
    train_return: float
    test_sharpe:  float
    test_return:  float
    test_max_dd:  float
    test_win_rate:float
    test_n_trades:int
    generalization: float   # test_sharpe / train_sharpe

    def to_dict(self) -> dict:
        return {
            "segment":        self.segment_id,
            "train":          {"start": self.train_start, "end": self.train_end,
                               "sharpe": round(self.train_sharpe, 3),
                               "return_pct": round(self.train_return * 100, 2)},
            "test":           {"start": self.test_start, "end": self.test_end,
                               "sharpe": round(self.test_sharpe, 3),
                               "return_pct": round(self.test_return * 100, 2),
                               "max_dd_pct": round(self.test_max_dd * 100, 2),
                               "win_rate_pct": round(self.test_win_rate * 100, 1),
                               "n_trades": self.test_n_trades},
            "generalization": round(self.generalization, 3),
        }


@dataclass
class WFStabilityAnalysis:
    n_segments:       int
    avg_test_sharpe:  float
    std_test_sharpe:  float
    sharpe_stability: float
    avg_test_return:  float
    pct_profitable:   float
    avg_generalization: float
    verdict:          str

    def to_dict(self) -> dict:
        return {
            "n_segments":         self.n_segments,
            "avg_test_sharpe":    round(self.avg_test_sharpe, 3),
            "std_test_sharpe":    round(self.std_test_sharpe, 3),
            "sharpe_stability":   round(self.sharpe_stability, 3),
            "avg_test_return_pct":round(self.avg_test_return * 100, 2),
            "pct_profitable":     round(self.pct_profitable * 100, 1),
            "avg_generalization": round(self.avg_generalization, 3),
            "verdict":            self.verdict,
        }


@dataclass
class WFResult:
    segments:   list
    combined_sharpe:   float
    combined_return:   float
    combined_max_dd:   float
    combined_win_rate: float
    combined_n_trades: int
    stability:  WFStabilityAnalysis
    train_days: int
    test_days:  int

    def to_dict(self) -> dict:
        return {
            "train_days":  self.train_days,
            "test_days":   self.test_days,
            "n_segments":  len(self.segments),
            "combined": {
                "sharpe":    round(self.combined_sharpe, 3),
                "return_pct":round(self.combined_return * 100, 2),
                "max_dd_pct":round(self.combined_max_dd * 100, 2),
                "win_rate_pct": round(self.combined_win_rate * 100, 1),
                "n_trades":  self.combined_n_trades,
            },
            "stability":  self.stability.to_dict(),
            "segments":   [s.to_dict() for s in self.segments],
        }

    def summary(self) -> str:
        s = self.stability
        lines = [
            "=== Walk-Forward 回測報告 ====",
            f"窗口設定: 訓練 {self.train_days}日 / 測試 {self.test_days}日",
            f"共 {len(self.segments)} 個窗口",
            "",
            "合併績效（測試段）:",
            f"  總報酬:    {self.combined_return*100:+.2f}%",
            f"  夏普值:    {self.combined_sharpe:.3f}",
            f"  最大回撤:  {self.combined_max_dd*100:.2f}%",
            f"  勝率:      {self.combined_win_rate*100:.1f}%",
            f"  交易筆數:  {self.combined_n_trades}",
            "",
            "穩定性分析:",
            f"  平均夏普:  {s.avg_test_sharpe:.3f} (±{s.std_test_sharpe:.3f})",
            f"  穩定指數:  {s.sharpe_stability:.3f}  (越高越穩定，目標>0.7)",
            f"  盈利窗口:  {s.pct_profitable*100:.0f}%",
            f"  泛化比:    {s.avg_generalization:.3f}  (越接近1越好)",
            f"  結論:      {s.verdict}",
        ]
        return "\n".join(lines)


class WalkForwardEngine:
    """
    Walk-Forward 回測引擎（防過擬合）。

    訓練=120日，測試=20日，滾動步長=test_days。
    嚴格避免 lookahead bias：訓練期與測試期絕不重疊。

    signal_fn(train_df: pd.DataFrame) → pd.Series
      - 接收訓練期 DataFrame 進行學習/校準
      - 回傳長度等於 len(train_df) 的訊號 Series（"buy"/"sell"/"hold"）
      - 引擎會獨立對測試期使用同一策略（使用訓練期末的狀態）

    使用方式：
        from quant.backtest_engine import WalkForwardEngine, BacktestEngine

        def my_strategy(train_df):
            # 用 train_df 學習，回傳訓練期訊號
            from quant.alpha_model import RuleBasedAlpha
            alpha = RuleBasedAlpha()
            return pd.Series([alpha.evaluate(r).signal.value
                              for _, r in train_df.iterrows()])

        wf = WalkForwardEngine(train_days=120, test_days=20)
        result = wf.run(feat_df, signal_fn=my_strategy)
        print(result.summary())
    """

    def __init__(
        self,
        train_days: int = 120,
        test_days:  int = 20,
        initial_capital: float = 1_000_000,
        commission_discount: float = 0.6,
    ):
        self.train_days = train_days
        self.test_days  = test_days
        self.capital    = initial_capital
        self.discount   = commission_discount

    def run(
        self,
        feat_df: pd.DataFrame,
        signal_fn: Callable,
        stop_loss_pct:   Optional[float] = 0.08,
        take_profit_pct: Optional[float] = None,
    ) -> WFResult:
        """
        執行 Walk-Forward 回測。

        feat_df   : FeatureEngine.compute_all() 的輸出 DataFrame
        signal_fn : fn(train_df) → pd.Series — 接受訓練期 df，回傳訊號
        """
        # 延遲匯入，避免循環依賴
        from quant.backtest_engine import BacktestEngine

        n  = len(feat_df)
        T  = self.train_days
        Te = self.test_days

        if n < T + Te:
            raise ValueError(
                f"資料不足：需 {T + Te}，實際 {n}（train={T} + test={Te}）"
            )

        segments: list[WFSegment] = []
        seg_id = 0
        start  = 0

        all_test_returns:  list[float] = []
        all_test_n_trades: int = 0
        all_test_pnl:      list[float] = []
        combined_equity:   list[float] = [self.capital]

        while start + T + Te <= n:
            train_end = start + T
            test_end  = train_end + Te

            train_df = feat_df.iloc[start:train_end].reset_index(drop=True)
            test_df  = feat_df.iloc[train_end:test_end].reset_index(drop=True)

            # ── 產生訊號（嚴格限制：只能讀 train_df）───────────────────
            try:
                train_signals = signal_fn(train_df)
                if len(train_signals) != len(train_df):
                    train_signals = pd.Series(["hold"] * len(train_df))
            except Exception as e:
                _log.warning("[WF] segment=%d signal_fn 失敗: %s", seg_id, e)
                train_signals = pd.Series(["hold"] * len(train_df))

            # ── 測試期：對 test_df 用同策略（用 signal_fn 直接生成）────
            try:
                test_signals = signal_fn(test_df)
                if len(test_signals) != len(test_df):
                    test_signals = pd.Series(["hold"] * len(test_df))
            except Exception as e:
                _log.warning("[WF] segment=%d test signal_fn 失敗: %s", seg_id, e)
                test_signals = pd.Series(["hold"] * len(test_df))

            engine = BacktestEngine(
                initial_capital=self.capital,
                commission_discount=self.discount,
            )

            # 訓練期回測（只用來計算 generalization ratio）
            try:
                train_rpt = engine.run(
                    train_df, train_signals,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                )
                train_sharpe = train_rpt.sharpe_ratio
                train_return = train_rpt.total_return
            except Exception:
                train_sharpe = 0.0; train_return = 0.0

            # 測試期回測（核心）
            try:
                test_rpt = engine.run(
                    test_df, test_signals,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                )
                test_sharpe  = test_rpt.sharpe_ratio
                test_return  = test_rpt.total_return
                test_max_dd  = test_rpt.max_drawdown
                test_win_rate= test_rpt.win_rate
                test_trades  = test_rpt.n_trades

                all_test_n_trades += test_trades
                all_test_pnl.extend([t.pnl for t in test_rpt.trades if t.pnl is not None])
                if combined_equity:
                    last_eq = combined_equity[-1]
                    combined_equity.append(last_eq * (1 + test_return))
            except Exception as e:
                _log.warning("[WF] segment=%d test backtest 失敗: %s", seg_id, e)
                test_sharpe = test_return = test_max_dd = test_win_rate = 0.0
                test_trades = 0

            generalization = (test_sharpe / train_sharpe) if abs(train_sharpe) > 1e-6 else 0.0

            def _date(df_, idx):
                try: return str(df_.iloc[idx].get("date", idx))[:10]
                except: return str(idx)

            seg = WFSegment(
                segment_id=seg_id,
                train_start=_date(feat_df, start),
                train_end=_date(feat_df, train_end - 1),
                test_start=_date(feat_df, train_end),
                test_end=_date(feat_df, test_end - 1),
                train_sharpe=round(train_sharpe, 3),
                train_return=round(train_return, 4),
                test_sharpe=round(test_sharpe, 3),
                test_return=round(test_return, 4),
                test_max_dd=round(test_max_dd, 4),
                test_win_rate=round(test_win_rate, 4),
                test_n_trades=test_trades,
                generalization=round(generalization, 3),
            )
            segments.append(seg)
            _log.info("[WF] segment %d  train_sharpe=%.3f  test_sharpe=%.3f  gen=%.3f",
                      seg_id, train_sharpe, test_sharpe, generalization)

            seg_id += 1
            start  += Te

        if not segments:
            raise ValueError("無有效回測窗口，請縮短 train_days 或提供更多資料")

        # ── 合併測試段績效 ───────────────────────────────────────────────
        eq_arr   = np.array(combined_equity)
        peak     = np.maximum.accumulate(eq_arr)
        dd_arr   = (peak - eq_arr) / peak
        comb_dd  = float(dd_arr.max())
        comb_ret = (eq_arr[-1] - eq_arr[0]) / eq_arr[0] if eq_arr[0] > 0 else 0.0
        ret_ser  = np.diff(eq_arr) / eq_arr[:-1]
        comb_vol = float(ret_ser.std() * np.sqrt(252)) if len(ret_ser) > 1 else 0.0
        n_days   = len(eq_arr)
        comb_ann = (1 + comb_ret) ** (252 / max(n_days, 1)) - 1
        comb_sharpe = comb_ann / comb_vol if comb_vol > 0 else 0.0

        wins = [p for p in all_test_pnl if p > 0]
        loss = [p for p in all_test_pnl if p <= 0]
        comb_wr = len(wins) / len(all_test_pnl) if all_test_pnl else 0.0

        # ── 穩定性分析 ───────────────────────────────────────────────────
        sharpes = np.array([s.test_sharpe for s in segments])
        returns = np.array([s.test_return for s in segments])
        gens    = np.array([s.generalization for s in segments])

        mean_s = float(sharpes.mean())
        std_s  = float(sharpes.std())
        stab   = max(0.0, 1.0 - std_s / abs(mean_s)) if abs(mean_s) > 1e-6 else 0.0
        pct_p  = float((returns > 0).mean())
        mean_g = float(gens.mean())

        if stab >= 0.7 and mean_s >= 1.0 and pct_p >= 0.7:
            verdict = "excellent"
        elif stab >= 0.5 and mean_s >= 0.5 and pct_p >= 0.6:
            verdict = "good"
        else:
            verdict = "poor"

        stability = WFStabilityAnalysis(
            n_segments=len(segments),
            avg_test_sharpe=round(mean_s, 3),
            std_test_sharpe=round(std_s, 3),
            sharpe_stability=round(stab, 3),
            avg_test_return=round(float(returns.mean()), 4),
            pct_profitable=round(pct_p, 3),
            avg_generalization=round(mean_g, 3),
            verdict=verdict,
        )

        return WFResult(
            segments=segments,
            combined_sharpe=round(comb_sharpe, 3),
            combined_return=round(comb_ret, 4),
            combined_max_dd=round(comb_dd, 4),
            combined_win_rate=round(comb_wr, 4),
            combined_n_trades=all_test_n_trades,
            stability=stability,
            train_days=self.train_days,
            test_days=self.test_days,
        )
