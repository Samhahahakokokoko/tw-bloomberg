"""
feature_engine.py — 技術特徵計算引擎

輸入：OHLCV DataFrame（欄位：date, open, high, low, close, volume）
輸出：附加完整技術特徵的 DataFrame

特徵分類：
  趨勢類    MA5 / MA10 / MA20 / MA60 / MA200、EMA12 / EMA26
  動能類    RSI14、MACD / Signal / Hist、KD（%K / %D）
  波動類    Bollinger Bands（上軌 / 中軌 / 下軌）、ATR14
  量能類    OBV、成交量比（vol_ratio）、量價乖離
  報酬類    1d / 5d / 10d / 20d 報酬率、超額報酬（相對 MA20）
  輔助類    高低比、實體比（蠟燭分析）、布林 %B
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional


class FeatureEngine:
    """
    技術特徵計算器。

    使用方式：
        fe = FeatureEngine(df)
        feat_df = fe.compute_all()

    df 必須包含 columns: date, open, high, low, close, volume
    date 欄位可為 str 或 datetime，都會自動轉換。
    """

    REQUIRED_COLS = {"open", "high", "low", "close", "volume"}

    def __init__(self, df: pd.DataFrame):
        missing = self.REQUIRED_COLS - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame 缺少欄位: {missing}")

        self.df = df.copy()
        # 確保數值型態
        for col in self.REQUIRED_COLS:
            self.df[col] = pd.to_numeric(self.df[col], errors="coerce")
        # 排序：確保時間序列正確
        if "date" in self.df.columns:
            self.df["date"] = pd.to_datetime(self.df["date"])
            self.df = self.df.sort_values("date").reset_index(drop=True)

    # ── 趨勢指標 ────────────────────────────────────────────────────────────

    def add_moving_averages(self) -> "FeatureEngine":
        """簡單移動平均線 MA5 / MA10 / MA20 / MA60 / MA200"""
        close = self.df["close"]
        for n in [5, 10, 20, 60, 200]:
            self.df[f"ma{n}"] = close.rolling(n, min_periods=1).mean()
        return self

    def add_ema(self) -> "FeatureEngine":
        """指數移動平均 EMA12 / EMA26"""
        close = self.df["close"]
        self.df["ema12"] = close.ewm(span=12, adjust=False).mean()
        self.df["ema26"] = close.ewm(span=26, adjust=False).mean()
        return self

    def add_ma_cross_signals(self) -> "FeatureEngine":
        """
        均線多頭排列訊號
          ma5_above_ma20: MA5 > MA20（短線偏多）
          ma20_above_ma60: MA20 > MA60（中線偏多）
          golden_cross: MA5 從下穿越 MA20（黃金交叉）
          death_cross:  MA5 從上穿越 MA20（死亡交叉）
        """
        if "ma5" not in self.df.columns:
            self.add_moving_averages()
        self.df["ma5_above_ma20"]  = (self.df["ma5"] > self.df["ma20"]).astype(int)
        self.df["ma20_above_ma60"] = (self.df["ma20"] > self.df["ma60"]).astype(int)
        # 前一日 MA5 < MA20，今日 MA5 > MA20 → 黃金交叉
        prev_below = self.df["ma5"].shift(1) < self.df["ma20"].shift(1)
        curr_above = self.df["ma5"] > self.df["ma20"]
        self.df["golden_cross"] = (prev_below & curr_above).astype(int)
        self.df["death_cross"]  = (~prev_below & ~curr_above).astype(int)
        return self

    # ── 動能指標 ────────────────────────────────────────────────────────────

    def add_rsi(self, period: int = 14) -> "FeatureEngine":
        """RSI 相對強弱指數"""
        delta = self.df["close"].diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        # Wilder 平滑（等同 EMA with alpha=1/period）
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        self.df[f"rsi{period}"] = 100 - (100 / (1 + rs))
        return self

    def add_macd(self, fast: int = 12, slow: int = 26, signal: int = 9) -> "FeatureEngine":
        """MACD 指數平滑異同移動平均"""
        close = self.df["close"]
        ema_fast   = close.ewm(span=fast,   adjust=False).mean()
        ema_slow   = close.ewm(span=slow,   adjust=False).mean()
        macd_line  = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        self.df["macd"]        = macd_line
        self.df["macd_signal"] = signal_line
        self.df["macd_hist"]   = macd_line - signal_line
        # 金叉 / 死叉
        self.df["macd_golden"] = (
            (self.df["macd"].shift(1) < self.df["macd_signal"].shift(1)) &
            (self.df["macd"] > self.df["macd_signal"])
        ).astype(int)
        return self

    def add_kd(self, k_period: int = 9, d_period: int = 3) -> "FeatureEngine":
        """KD 隨機指標（台灣慣用：9K / 3D）"""
        low_min  = self.df["low"].rolling(k_period, min_periods=1).min()
        high_max = self.df["high"].rolling(k_period, min_periods=1).max()
        rsv = (self.df["close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
        rsv = rsv.fillna(50)
        # K = 前K × (d-1)/d + RSV × 1/d  (d=3)
        k_vals, d_vals = [], []
        k_prev, d_prev = 50.0, 50.0
        for r in rsv:
            k = k_prev * (d_period - 1) / d_period + r / d_period
            d = d_prev * (d_period - 1) / d_period + k / d_period
            k_vals.append(k)
            d_vals.append(d)
            k_prev, d_prev = k, d
        self.df["k"] = k_vals
        self.df["d"] = d_vals
        self.df["j"] = 3 * self.df["k"] - 2 * self.df["d"]
        return self

    # ── 波動指標 ────────────────────────────────────────────────────────────

    def add_bollinger(self, period: int = 20, std_mult: float = 2.0) -> "FeatureEngine":
        """布林通道"""
        ma  = self.df["close"].rolling(period, min_periods=1).mean()
        std = self.df["close"].rolling(period, min_periods=1).std()
        self.df["boll_mid"]   = ma
        self.df["boll_upper"] = ma + std_mult * std
        self.df["boll_lower"] = ma - std_mult * std
        # %B：0=下軌，1=上軌，>1=突破上軌，<0=跌破下軌
        band = self.df["boll_upper"] - self.df["boll_lower"]
        self.df["boll_b"] = (self.df["close"] - self.df["boll_lower"]) / band.replace(0, np.nan)
        # 帶寬：越寬代表波動越大
        self.df["boll_width"] = band / ma
        return self

    def add_atr(self, period: int = 14) -> "FeatureEngine":
        """ATR 真實波幅（衡量波動度，用於動態停損）"""
        high  = self.df["high"]
        low   = self.df["low"]
        close = self.df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        self.df[f"atr{period}"] = tr.ewm(alpha=1 / period, adjust=False).mean()
        return self

    # ── 量能指標 ────────────────────────────────────────────────────────────

    def add_obv(self) -> "FeatureEngine":
        """OBV 能量潮（On-Balance Volume）"""
        direction = np.sign(self.df["close"].diff().fillna(0))
        self.df["obv"] = (direction * self.df["volume"]).cumsum()
        return self

    def add_volume_features(self) -> "FeatureEngine":
        """
        量能衍生特徵：
          vol_ma5:   5日均量
          vol_ratio: 今日量 / 5日均量（>1.5 代表放量）
          vol_price_trend: OBV 5日斜率（量能趨勢）
        """
        self.df["vol_ma5"]   = self.df["volume"].rolling(5, min_periods=1).mean()
        self.df["vol_ratio"] = self.df["volume"] / self.df["vol_ma5"].replace(0, np.nan)
        if "obv" not in self.df.columns:
            self.add_obv()
        # OBV 5日線性斜率（正 = 量能遞增）
        obv = self.df["obv"]
        self.df["obv_slope5"] = obv.diff(5) / 5
        return self

    # ── 報酬特徵 ────────────────────────────────────────────────────────────

    def add_return_features(self) -> "FeatureEngine":
        """
        各期間報酬率與超額報酬：
          ret_1d / ret_5d / ret_10d / ret_20d
          excess_ret_5d: 5日報酬 - MA20 偏離程度
        """
        close = self.df["close"]
        for n in [1, 5, 10, 20]:
            self.df[f"ret_{n}d"] = close.pct_change(n)
        if "ma20" not in self.df.columns:
            self.add_moving_averages()
        # 超額報酬：距 MA20 的偏離幅度（>0 = 強於均線）
        self.df["excess_ret"] = (close - self.df["ma20"]) / self.df["ma20"]
        return self

    # ── 蠟燭形態輔助特徵 ────────────────────────────────────────────────────

    def add_candle_features(self) -> "FeatureEngine":
        """
        蠟燭體特徵（用於模型輸入，不直接產生訊號）：
          body_ratio:  實體比 = |收-開| / (高-低)，越大代表方向性越強
          upper_shadow: 上影線比
          lower_shadow: 下影線比
          hl_ratio:    (高-低) / 低，衡量當日振幅
        """
        o, h, l, c = self.df["open"], self.df["high"], self.df["low"], self.df["close"]
        rng = (h - l).replace(0, np.nan)
        self.df["body_ratio"]   = (c - o).abs() / rng
        self.df["upper_shadow"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / rng
        self.df["lower_shadow"] = (pd.concat([o, c], axis=1).min(axis=1) - l) / rng
        self.df["hl_ratio"]     = rng / l
        return self

    # ── 一鍵計算所有特徵 ────────────────────────────────────────────────────

    def compute_all(self) -> pd.DataFrame:
        """計算全部特徵並回傳 DataFrame（會有部分 NaN，視 rolling 需要）"""
        (
            self
            .add_moving_averages()
            .add_ema()
            .add_ma_cross_signals()
            .add_rsi()
            .add_macd()
            .add_kd()
            .add_bollinger()
            .add_atr()
            .add_obv()
            .add_volume_features()
            .add_return_features()
            .add_candle_features()
        )
        return self.df

    @property
    def feature_columns(self) -> list[str]:
        """回傳所有特徵欄位（排除原始 OHLCV + date）"""
        raw = {"date", "open", "high", "low", "close", "volume"}
        return [c for c in self.df.columns if c not in raw]


# ── Mock 資料 + 獨立測試 ────────────────────────────────────────────────────

def _generate_mock_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """產生 n 筆模擬 OHLCV 資料（隨機漫步）"""
    rng = np.random.default_rng(seed)
    dates  = pd.date_range("2023-01-01", periods=n, freq="B")  # 交易日
    close  = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    noise  = rng.uniform(0.995, 1.005, n)
    high   = close * rng.uniform(1.000, 1.03, n)
    low    = close * rng.uniform(0.97,  1.000, n)
    open_  = close * noise
    volume = rng.integers(5_000_000, 50_000_000, n).astype(float)
    return pd.DataFrame({
        "date":   dates,
        "open":   open_,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": volume,
    })


if __name__ == "__main__":
    mock_df = _generate_mock_ohlcv(300)
    fe  = FeatureEngine(mock_df)
    out = fe.compute_all()
    print(f"[FeatureEngine] 共計算 {len(fe.feature_columns)} 個特徵")
    print(out[fe.feature_columns].tail(5).to_string())
