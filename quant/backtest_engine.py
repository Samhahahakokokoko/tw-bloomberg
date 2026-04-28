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
