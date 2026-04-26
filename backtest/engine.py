"""回測引擎 — 支援 MA交叉、RSI、MACD、KD 策略"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
import pandas as pd
import numpy as np
from loguru import logger


StrategyType = Literal["ma_cross", "rsi", "macd", "kd", "bollinger", "pvd", "institutional"]


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
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, df: pd.DataFrame, initial_capital: float = 1_000_000):
        """
        df 需含欄位: date, open, high, low, close, volume
        """
        self.df = df.copy()
        self.df["date"] = pd.to_datetime(self.df["date"])
        self.df.sort_values("date", inplace=True)
        self.df.reset_index(drop=True, inplace=True)
        self.initial_capital = initial_capital

    def run(self, strategy: StrategyType, **params) -> BacktestResult:
        signals = self._generate_signals(strategy, **params)
        return self._simulate(strategy, signals)

    def _generate_signals(self, strategy: StrategyType, **params) -> pd.Series:
        df = self.df
        if strategy == "ma_cross":
            short = params.get("short", 5)
            long_ = params.get("long", 20)
            df["ma_short"] = df["close"].rolling(short).mean()
            df["ma_long"] = df["close"].rolling(long_).mean()
            signal = pd.Series(0, index=df.index)
            signal[(df["ma_short"] > df["ma_long"]) & (df["ma_short"].shift(1) <= df["ma_long"].shift(1))] = 1
            signal[(df["ma_short"] < df["ma_long"]) & (df["ma_short"].shift(1) >= df["ma_long"].shift(1))] = -1
            return signal

        if strategy == "rsi":
            period = params.get("period", 14)
            overbought = params.get("overbought", 70)
            oversold = params.get("oversold", 30)
            delta = df["close"].diff()
            gain = delta.clip(lower=0).rolling(period).mean()
            loss = (-delta.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, np.nan)
            df["rsi"] = 100 - 100 / (1 + rs)
            signal = pd.Series(0, index=df.index)
            signal[(df["rsi"] < oversold) & (df["rsi"].shift(1) >= oversold)] = 1
            signal[(df["rsi"] > overbought) & (df["rsi"].shift(1) <= overbought)] = -1
            return signal

        if strategy == "macd":
            fast = params.get("fast", 12)
            slow = params.get("slow", 26)
            signal_period = params.get("signal", 9)
            ema_fast = df["close"].ewm(span=fast).mean()
            ema_slow = df["close"].ewm(span=slow).mean()
            df["macd"] = ema_fast - ema_slow
            df["macd_signal"] = df["macd"].ewm(span=signal_period).mean()
            signal = pd.Series(0, index=df.index)
            signal[(df["macd"] > df["macd_signal"]) & (df["macd"].shift(1) <= df["macd_signal"].shift(1))] = 1
            signal[(df["macd"] < df["macd_signal"]) & (df["macd"].shift(1) >= df["macd_signal"].shift(1))] = -1
            return signal

        if strategy == "kd":
            period = params.get("period", 9)
            k_period = params.get("k_period", 3)
            d_period = params.get("d_period", 3)
            low_min = df["low"].rolling(period).min()
            high_max = df["high"].rolling(period).max()
            rsv = (df["close"] - low_min) / (high_max - low_min + 1e-9) * 100
            df["k"] = rsv.ewm(alpha=1 / k_period, adjust=False).mean()
            df["d"] = df["k"].ewm(alpha=1 / d_period, adjust=False).mean()
            signal = pd.Series(0, index=df.index)
            signal[(df["k"] > df["d"]) & (df["k"].shift(1) <= df["d"].shift(1)) & (df["k"] < 20)] = 1
            signal[(df["k"] < df["d"]) & (df["k"].shift(1) >= df["d"].shift(1)) & (df["k"] > 80)] = -1
            return signal

        if strategy == "bollinger":
            period = params.get("period", 20)
            std_mult = params.get("std_mult", 2.0)
            df["bb_mid"] = df["close"].rolling(period).mean()
            df["bb_std"] = df["close"].rolling(period).std()
            df["bb_upper"] = df["bb_mid"] + std_mult * df["bb_std"]
            df["bb_lower"] = df["bb_mid"] - std_mult * df["bb_std"]
            signal = pd.Series(0, index=df.index)
            # 買：收盤從下穿越布林下軌（反彈）
            signal[
                (df["close"] > df["bb_lower"]) &
                (df["close"].shift(1) <= df["bb_lower"].shift(1))
            ] = 1
            # 賣：收盤觸碰布林上軌
            signal[
                (df["close"] >= df["bb_upper"]) &
                (df["close"].shift(1) < df["bb_upper"].shift(1))
            ] = -1
            return signal

        if strategy == "pvd":
            # 價量背離：
            #   背離買進 — 價格創新低但成交量萎縮（底部訊號）
            #   背離賣出 — 價格創新高但成交量萎縮（頭部訊號）
            period = params.get("period", 10)
            vol_ma = df["volume"].rolling(period).mean()
            price_new_low = df["close"] == df["close"].rolling(period).min()
            price_new_high = df["close"] == df["close"].rolling(period).max()
            vol_shrink = df["volume"] < vol_ma * 0.7   # 成交量低於均量 70%
            signal = pd.Series(0, index=df.index)
            signal[price_new_low & vol_shrink & (df["close"] > df["close"].shift(1))] = 1
            signal[price_new_high & vol_shrink & (df["close"] < df["close"].shift(1))] = -1
            return signal

        if strategy == "institutional":
            # 籌碼面：外資連續買超 N 日買進，連續賣超 M 日賣出
            # 以 foreign_net 欄位（若無則用 volume spike 代替）
            consec_buy = params.get("consec_buy", 3)
            consec_sell = params.get("consec_sell", 2)
            signal = pd.Series(0, index=df.index)

            if "foreign_net" in df.columns and df["foreign_net"].notna().sum() > consec_buy:
                fn = df["foreign_net"].fillna(0)
                # 滾動視窗：連續 N 日外資淨買
                buy_streak = (fn > 0).rolling(consec_buy).sum() == consec_buy
                sell_streak = (fn < 0).rolling(consec_sell).sum() == consec_sell
                signal[buy_streak & ~buy_streak.shift(1, fill_value=False)] = 1
                signal[sell_streak & ~sell_streak.shift(1, fill_value=False)] = -1
            else:
                # Fallback：外資資料不足時，用 MACD 代替
                ema_fast = df["close"].ewm(span=12).mean()
                ema_slow = df["close"].ewm(span=26).mean()
                df["_macd"] = ema_fast - ema_slow
                df["_sig"] = df["_macd"].ewm(span=9).mean()
                signal[(df["_macd"] > df["_sig"]) & (df["_macd"].shift(1) <= df["_sig"].shift(1))] = 1
                signal[(df["_macd"] < df["_sig"]) & (df["_macd"].shift(1) >= df["_sig"].shift(1))] = -1

            return signal

        raise ValueError(f"Unknown strategy: {strategy}")

    def _simulate(self, strategy: str, signals: pd.Series) -> BacktestResult:
        df = self.df
        capital = self.initial_capital
        position = 0
        entry_price = 0.0
        trades = []
        equity = []

        for i, (_, row) in enumerate(df.iterrows()):
            sig = signals.iloc[i]
            price = float(row["close"])
            date_str = str(row["date"].date())

            if sig == 1 and position == 0:
                # 用 95% 資金買入，以股數計（回測不限整張）
                shares = int(capital * 0.95 / price)
                if shares > 0:
                    cost = shares * price
                    capital -= cost
                    position = shares
                    entry_price = price
                    trades.append({"date": date_str, "action": "BUY", "price": price, "shares": shares})

            elif sig == -1 and position > 0:
                proceeds = position * price
                capital += proceeds
                pnl = (price - entry_price) * position
                trades.append({"date": date_str, "action": "SELL", "price": price, "shares": position, "pnl": pnl})
                position = 0

            equity.append({"date": date_str, "value": capital + position * price})

        # Close open position at end
        if position > 0:
            price = float(df["close"].iloc[-1])
            capital += position * price

        total_return = (capital - self.initial_capital) / self.initial_capital * 100
        days = (df["date"].iloc[-1] - df["date"].iloc[0]).days or 1
        annualized = ((1 + total_return / 100) ** (365 / days) - 1) * 100

        sell_trades = [t for t in trades if t.get("action") == "SELL"]
        win_trades = [t for t in sell_trades if t.get("pnl", 0) > 0]
        win_rate = len(win_trades) / len(sell_trades) * 100 if sell_trades else 0

        equity_vals = [e["value"] for e in equity]
        peak = pd.Series(equity_vals).cummax()
        drawdown = ((pd.Series(equity_vals) - peak) / peak * 100).min()

        returns = pd.Series(equity_vals).pct_change().dropna()
        sharpe = float(returns.mean() / returns.std() * (252 ** 0.5)) if returns.std() else 0

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
            trades=trades,
            equity_curve=equity,
        )
