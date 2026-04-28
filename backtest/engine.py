"""回測引擎 v2 — 台股真實交易成本模型

新增：
  - 手續費 0.1425%（買賣雙邊）
  - 交易稅 0.3%（賣出才收）
  - 滑價 slippage 0.05%
  - 台股漲跌停限制 ±10%
  - 成交量限制：單筆成交量 < 1% 日成交量才成交
  - 市場盤態偵測（Multi-regime）
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
import uuid
import pandas as pd
import numpy as np
from loguru import logger

StrategyType = Literal[
    "ma_cross", "rsi", "macd", "kd", "bollinger",
    "pvd", "institutional", "momentum", "mean_reversion", "defensive",
]

# ── 台股交易成本常數 ──────────────────────────────────────────────────────────
COMMISSION    = 0.001425   # 手續費（買賣各收）
TAX           = 0.003      # 證交稅（賣出收）
SLIPPAGE      = 0.0005     # 市場衝擊/滑價（單邊）
LIMIT_UP_DOWN = 0.10       # 台股漲跌停 ±10%
MAX_VOL_RATIO = 0.01       # 單筆不超過日成交量 1%


def _buy_cost(shares: int, price: float) -> float:
    """買進實際付出金額"""
    return shares * price * (1 + COMMISSION + SLIPPAGE)


def _sell_proceeds(shares: int, price: float) -> float:
    """賣出實際收到金額"""
    return shares * price * (1 - COMMISSION - TAX - SLIPPAGE)


def _apply_limit(price: float, prev_close: float) -> float:
    """套用台股漲跌停，回傳有效成交價"""
    upper = prev_close * (1 + LIMIT_UP_DOWN)
    lower = prev_close * (1 - LIMIT_UP_DOWN)
    return max(lower, min(upper, price))


@dataclass
class BacktestResult:
    strategy: str
    stock_code: str
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe_ratio: float
    total_trades: int
    win_rate: float
    # 成本明細
    total_commission: float = 0.0
    total_tax: float        = 0.0
    total_slippage: float   = 0.0
    total_cost_impact: float = 0.0   # 成本對報酬的影響（%）
    # 市場盤態
    regime: str = ""          # bull / bear / sideways
    regime_pct: dict = field(default_factory=dict)
    # 交易記錄
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    session_id: str = ""


class BacktestEngine:
    def __init__(self, df: pd.DataFrame, initial_capital: float = 1_000_000):
        """df 需含: date, open, high, low, close, volume"""
        self.df = df.copy()
        self.df["date"] = pd.to_datetime(self.df["date"])
        self.df.sort_values("date", inplace=True)
        self.df.reset_index(drop=True, inplace=True)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in self.df.columns:
                self.df[col] = pd.to_numeric(self.df[col], errors="coerce").fillna(0)
        self.initial_capital = initial_capital
        self.session_id = str(uuid.uuid4())[:8]

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def run(self, strategy: StrategyType, **params) -> BacktestResult:
        signals = self._generate_signals(strategy, **params)
        regime  = detect_market_regime(self.df)
        result  = self._simulate(strategy, signals)
        result.regime = regime["current"]
        result.regime_pct = regime["pct"]
        result.session_id = self.session_id
        return result

    def detect_regime(self) -> dict:
        return detect_market_regime(self.df)

    # ── 訊號產生 ─────────────────────────────────────────────────────────────

    def _generate_signals(self, strategy: StrategyType, **p) -> pd.Series:
        df = self.df
        signal = pd.Series(0, index=df.index)

        if strategy == "ma_cross":
            s, l = p.get("short", 5), p.get("long", 20)
            ms = df["close"].rolling(s).mean()
            ml = df["close"].rolling(l).mean()
            signal[(ms > ml) & (ms.shift(1) <= ml.shift(1))] = 1
            signal[(ms < ml) & (ms.shift(1) >= ml.shift(1))] = -1

        elif strategy == "rsi":
            period   = p.get("period", 14)
            obought  = p.get("overbought", 70)
            osold    = p.get("oversold", 30)
            delta    = df["close"].diff()
            gain     = delta.clip(lower=0).rolling(period).mean()
            loss     = (-delta.clip(upper=0)).rolling(period).mean()
            df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
            signal[(df["rsi"] < osold)   & (df["rsi"].shift(1) >= osold)]   = 1
            signal[(df["rsi"] > obought) & (df["rsi"].shift(1) <= obought)] = -1

        elif strategy == "macd":
            fast, slow, sig = p.get("fast", 12), p.get("slow", 26), p.get("signal", 9)
            ema_f = df["close"].ewm(span=fast).mean()
            ema_s = df["close"].ewm(span=slow).mean()
            df["macd"] = ema_f - ema_s
            df["macd_signal"] = df["macd"].ewm(span=sig).mean()
            signal[(df["macd"] > df["macd_signal"]) & (df["macd"].shift(1) <= df["macd_signal"].shift(1))] = 1
            signal[(df["macd"] < df["macd_signal"]) & (df["macd"].shift(1) >= df["macd_signal"].shift(1))] = -1

        elif strategy == "kd":
            period = p.get("period", 9)
            k_sm, d_sm = p.get("k_period", 3), p.get("d_period", 3)
            lo = df["low"].rolling(period).min()
            hi = df["high"].rolling(period).max()
            rsv = (df["close"] - lo) / (hi - lo + 1e-9) * 100
            df["k"] = rsv.ewm(alpha=1 / k_sm, adjust=False).mean()
            df["d"] = df["k"].ewm(alpha=1 / d_sm, adjust=False).mean()
            signal[(df["k"] > df["d"]) & (df["k"].shift(1) <= df["d"].shift(1)) & (df["k"] < 20)] = 1
            signal[(df["k"] < df["d"]) & (df["k"].shift(1) >= df["d"].shift(1)) & (df["k"] > 80)] = -1

        elif strategy == "bollinger":
            period = p.get("period", 20)
            mult   = p.get("std_mult", 2.0)
            mid    = df["close"].rolling(period).mean()
            std    = df["close"].rolling(period).std()
            upper, lower = mid + mult * std, mid - mult * std
            signal[(df["close"] > lower) & (df["close"].shift(1) <= lower.shift(1))] = 1
            signal[(df["close"] >= upper) & (df["close"].shift(1) < upper.shift(1))] = -1

        elif strategy == "pvd":
            period  = p.get("period", 10)
            vol_ma  = df["volume"].rolling(period).mean()
            p_lo    = df["close"] == df["close"].rolling(period).min()
            p_hi    = df["close"] == df["close"].rolling(period).max()
            v_shrink = df["volume"] < vol_ma * 0.7
            signal[p_lo & v_shrink & (df["close"] > df["close"].shift(1))] = 1
            signal[p_hi & v_shrink & (df["close"] < df["close"].shift(1))] = -1

        elif strategy == "institutional":
            cb, cs = p.get("consec_buy", 3), p.get("consec_sell", 2)
            if "foreign_net" in df.columns and df["foreign_net"].notna().sum() > cb:
                fn = df["foreign_net"].fillna(0)
                buy_s  = (fn > 0).rolling(cb).sum() == cb
                sell_s = (fn < 0).rolling(cs).sum() == cs
                signal[buy_s  & ~buy_s.shift(1,  fill_value=False)] = 1
                signal[sell_s & ~sell_s.shift(1, fill_value=False)] = -1
            else:
                return self._generate_signals("macd", **p)

        elif strategy == "momentum":
            # 多頭行情策略：動能追漲
            lb = p.get("lookback", 20)
            thresh = p.get("threshold", 0.05)
            ret = df["close"].pct_change(lb)
            signal[ret > thresh]  = 1
            signal[ret < -thresh] = -1

        elif strategy == "mean_reversion":
            # 盤整行情策略：均值回歸
            period = p.get("period", 20)
            mult   = p.get("std_mult", 1.5)
            mid    = df["close"].rolling(period).mean()
            std    = df["close"].rolling(period).std()
            signal[(df["close"] < mid - mult * std)] = 1
            signal[(df["close"] > mid + mult * std)] = -1

        elif strategy == "defensive":
            # 空頭行情策略：防禦型（低波動 + 高殖利率概念）
            # 簡化實作：只在 RSI < 35 才買，快速停利
            period = p.get("period", 14)
            delta = df["close"].diff()
            gain  = delta.clip(lower=0).rolling(period).mean()
            loss  = (-delta.clip(upper=0)).rolling(period).mean()
            df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
            signal[df["rsi"] < 35] = 1
            signal[df["rsi"] > 55] = -1

        else:
            raise ValueError(f"Unknown strategy: {strategy}")

        return signal

    # ── 模擬撮合（含真實成本）───────────────────────────────────────────────

    def _simulate(self, strategy: str, signals: pd.Series) -> BacktestResult:
        df      = self.df
        capital = float(self.initial_capital)
        position = 0
        entry_price = 0.0
        entry_date  = ""
        trades  = []
        equity  = []
        total_commission = total_tax = total_slippage = 0.0

        for i, (_, row) in enumerate(df.iterrows()):
            sig        = signals.iloc[i]
            raw_price  = float(row["close"])
            daily_vol  = float(row.get("volume", 0) or 0)
            date_str   = str(row["date"].date())

            # 套用漲跌停（用前日收盤）
            if i > 0:
                prev_close = float(df["close"].iloc[i - 1])
                trade_price = _apply_limit(raw_price, prev_close)
            else:
                trade_price = raw_price

            if sig == 1 and position == 0:
                # 最大成交量限制
                max_shares_by_vol = int(daily_vol * MAX_VOL_RATIO) if daily_vol > 0 else 10**6
                shares = min(
                    int(capital * 0.95 / trade_price),
                    max_shares_by_vol,
                )
                if shares > 0:
                    cost = _buy_cost(shares, trade_price)
                    if cost <= capital:
                        comm = shares * trade_price * COMMISSION
                        slip = shares * trade_price * SLIPPAGE
                        capital -= cost
                        position    = shares
                        entry_price = trade_price
                        entry_date  = date_str
                        total_commission += comm
                        total_slippage   += slip
                        trades.append({
                            "date":    date_str,
                            "action":  "BUY",
                            "price":   round(trade_price, 2),
                            "shares":  shares,
                            "cost":    round(cost, 0),
                            "commission": round(comm, 0),
                        })

            elif sig == -1 and position > 0:
                proceeds = _sell_proceeds(position, trade_price)
                comm     = position * trade_price * COMMISSION
                tax_amt  = position * trade_price * TAX
                slip     = position * trade_price * SLIPPAGE
                capital += proceeds
                gross_pnl = (trade_price - entry_price) * position
                net_pnl   = proceeds - _buy_cost(position, entry_price)
                total_commission += comm
                total_tax        += tax_amt
                total_slippage   += slip
                holding_days = max(1, (
                    pd.Timestamp(date_str) - pd.Timestamp(entry_date)
                ).days)
                peak_capital = capital + position * trade_price
                dd = min(0.0, (proceeds - capital) / peak_capital * 100) if peak_capital > 0 else 0.0
                trades.append({
                    "date":         date_str,
                    "action":       "SELL",
                    "price":        round(trade_price, 2),
                    "shares":       position,
                    "proceeds":     round(proceeds, 0),
                    "pnl":          round(net_pnl, 0),
                    "gross_pnl":    round(gross_pnl, 0),
                    "commission":   round(comm, 0),
                    "tax":          round(tax_amt, 0),
                    "slippage":     round(slip, 0),
                    "holding_days": holding_days,
                    "entry_date":   entry_date,
                    "entry_price":  round(entry_price, 2),
                })
                position = 0

            equity.append({"date": date_str, "value": capital + position * trade_price})

        # 強制平倉（終止日未出場的持股以最後收盤計算）
        if position > 0:
            last_price = float(df["close"].iloc[-1])
            capital += _sell_proceeds(position, last_price)

        total_return = (capital - self.initial_capital) / self.initial_capital * 100
        days = max(1, (df["date"].iloc[-1] - df["date"].iloc[0]).days)
        annualized = ((1 + total_return / 100) ** (365 / days) - 1) * 100

        sell_trades = [t for t in trades if t.get("action") == "SELL"]
        win_trades  = [t for t in sell_trades if (t.get("pnl") or 0) > 0]
        win_rate    = len(win_trades) / len(sell_trades) * 100 if sell_trades else 0

        ev = [e["value"] for e in equity]
        peak     = pd.Series(ev).cummax()
        drawdown = ((pd.Series(ev) - peak) / peak * 100).min()
        returns  = pd.Series(ev).pct_change().dropna()
        sharpe   = float(returns.mean() / returns.std() * (252 ** 0.5)) if returns.std() else 0

        # 成本對報酬的負面影響（%）
        total_cost_all = total_commission + total_tax + total_slippage
        cost_impact    = -total_cost_all / self.initial_capital * 100

        return BacktestResult(
            strategy=strategy,
            stock_code="",
            start_date=str(df["date"].iloc[0].date()),
            end_date=str(df["date"].iloc[-1].date()),
            initial_capital=self.initial_capital,
            final_capital=round(capital, 2),
            total_return=round(total_return, 2),
            annualized_return=round(annualized, 2),
            max_drawdown=round(float(drawdown), 2),
            sharpe_ratio=round(sharpe, 4),
            total_trades=len(trades),
            win_rate=round(win_rate, 2),
            total_commission=round(total_commission, 0),
            total_tax=round(total_tax, 0),
            total_slippage=round(total_slippage, 0),
            total_cost_impact=round(cost_impact, 3),
            trades=trades,
            equity_curve=equity,
        )


# ── 市場盤態偵測（獨立函式，可被其他模組呼叫）────────────────────────────────

def detect_market_regime(df: pd.DataFrame) -> dict:
    """
    偵測市場盤態：
      Bull     : close > MA200 AND MA5 > MA20
      Bear     : close < MA200 AND MA5 < MA20
      Sideways : 其他（MA5 ≈ MA20 或跨越 MA200）

    若資料不足 200 天，改用 MA60 代替 MA200。
    """
    closes = df["close"].values
    n = len(closes)
    if n < 20:
        return {"current": "unknown", "pct": {}, "ma5": None, "ma20": None, "ma200": None}

    ma5   = float(np.mean(closes[-5:]))
    ma20  = float(np.mean(closes[-20:]))
    ma200 = float(np.mean(closes[-min(200, n):])) if n >= 60 else None
    curr  = float(closes[-1])

    if ma200 is None:
        regime = "sideways"
    elif curr > ma200 and ma5 > ma20:
        regime = "bull"
    elif curr < ma200 and ma5 < ma20:
        regime = "bear"
    else:
        regime = "sideways"

    # 計算近 N 日各盤態佔比（用於統計）
    period   = min(n, 200)
    regimes  = []
    for i in range(period, n):
        window  = closes[:i]
        wma5    = float(np.mean(window[-5:]))
        wma20   = float(np.mean(window[-20:]))
        wma200  = float(np.mean(window[-min(200, len(window)):]))
        wc      = float(window[-1])
        if wc > wma200 and wma5 > wma20:
            regimes.append("bull")
        elif wc < wma200 and wma5 < wma20:
            regimes.append("bear")
        else:
            regimes.append("sideways")

    pct = {}
    if regimes:
        from collections import Counter
        counts = Counter(regimes)
        total  = len(regimes)
        pct    = {k: round(v / total * 100, 1) for k, v in counts.items()}

    return {
        "current": regime,
        "pct":     pct,
        "ma5":     round(ma5, 2),
        "ma20":    round(ma20, 2),
        "ma200":   round(ma200, 2) if ma200 else None,
        "close":   round(curr, 2),
    }


def recommend_strategy_for_regime(regime: str) -> str:
    """根據市場盤態推薦策略"""
    return {
        "bull":     "momentum",
        "bear":     "defensive",
        "sideways": "mean_reversion",
    }.get(regime, "macd")
