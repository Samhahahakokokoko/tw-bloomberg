"""
risk_engine.py — 風險管理引擎

功能：
  1. 市場盤態偵測（Regime Detection）
       Bull  / Bear / Sideways — 基於 MA200 + MA5/MA20 交叉
       Volatile / Quiet        — 基於 ATR 百分位數
  2. 最大回撤控制（Drawdown Control）
       追蹤最高淨值、即時回撤%、觸及閾值後降倉或停止交易
  3. 倉位風控（Position Risk）
       個股停損（固定% / ATR 倍數）、組合最大虧損、VAR 估算
  4. 盤態 → 策略建議對應表
       根據當前 Regime 自動推薦適合的策略

使用方式：
    re = RiskEngine(initial_capital=1_000_000)
    regime = re.detect_regime(price_df)
    ok = re.check_drawdown(current_equity)
    stop_price = re.calc_stop_loss(entry_price=850, atr=12.5)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── 列舉 ─────────────────────────────────────────────────────────────────────

class MarketRegime(str, Enum):
    BULL      = "bull"       # 多頭：MA5>MA20 且收盤>MA200
    BEAR      = "bear"       # 空頭：MA5<MA20 且收盤<MA200
    SIDEWAYS  = "sideways"   # 盤整：介於中間
    VOLATILE  = "volatile"   # 高波動（ATR > 80th 百分位）
    UNKNOWN   = "unknown"    # 資料不足


class DrawdownState(str, Enum):
    NORMAL   = "normal"    # 正常
    WARNING  = "warning"   # 回撤超過警戒線（預設 10%）
    CRITICAL = "critical"  # 回撤超過停損線（預設 20%）
    HALTED   = "halted"    # 已停止交易


# ── Regime 對應策略建議 ───────────────────────────────────────────────────────

REGIME_STRATEGY_MAP: dict[MarketRegime, dict] = {
    MarketRegime.BULL: {
        "strategies":   ["momentum", "ma_cross", "macd", "institutional"],
        "max_long_pct": 0.80,   # 最大多頭倉位
        "stop_loss":    0.08,   # 停損幅度（寬鬆）
        "tip":          "多頭趨勢，以順勢策略為主，適度放寬停損",
    },
    MarketRegime.BEAR: {
        "strategies":   ["defensive", "mean_reversion"],
        "max_long_pct": 0.30,   # 空頭大幅降低多頭倉位
        "stop_loss":    0.05,   # 停損幅度（嚴格）
        "tip":          "空頭環境，大幅降倉，嚴守停損",
    },
    MarketRegime.SIDEWAYS: {
        "strategies":   ["rsi", "bollinger", "mean_reversion", "kd"],
        "max_long_pct": 0.60,
        "stop_loss":    0.06,
        "tip":          "盤整格局，以均值回歸策略為主，嚴格設定目標價位",
    },
    MarketRegime.VOLATILE: {
        "strategies":   ["defensive"],
        "max_long_pct": 0.40,
        "stop_loss":    0.05,
        "tip":          "高波動環境，縮減倉位，等待波動收斂",
    },
    MarketRegime.UNKNOWN: {
        "strategies":   ["defensive"],
        "max_long_pct": 0.30,
        "stop_loss":    0.05,
        "tip":          "資料不足，保守操作",
    },
}

REGIME_DESC = {
    MarketRegime.BULL:     "多頭趨勢",
    MarketRegime.BEAR:     "空頭趨勢",
    MarketRegime.SIDEWAYS: "盤整整理",
    MarketRegime.VOLATILE: "高波動",
    MarketRegime.UNKNOWN:  "未知",
}


# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class RegimeResult:
    """盤態偵測結果"""
    regime:       MarketRegime
    description:  str
    confidence:   float             # 0~1，偵測信心度
    close:        float
    ma5:          float
    ma20:         float
    ma200:        Optional[float]
    atr14:        Optional[float]
    atr_pct:      Optional[float]   # ATR / close，衡量相對波動度
    strategies:   list[str]         # 建議策略
    max_long_pct: float
    stop_loss:    float
    tip:          str
    extra:        dict = field(default_factory=dict)


@dataclass
class DrawdownInfo:
    """回撤追蹤資訊"""
    peak_equity:    float           # 歷史最高淨值
    current_equity: float
    drawdown_pct:   float           # 當前回撤%（正值 = 虧損中）
    max_drawdown:   float           # 本次策略運行最大回撤
    state:          DrawdownState
    warning_pct:    float           # 警戒線
    critical_pct:   float           # 停損線


# ── 風險引擎 ─────────────────────────────────────────────────────────────────

class RiskEngine:
    """
    量化交易風險管理引擎。

    使用方式：
        re = RiskEngine(initial_capital=1_000_000)

        # 盤態偵測
        regime = re.detect_regime(df)  # df = OHLCV DataFrame

        # 回撤控制
        re.update_equity(current_equity)
        info = re.drawdown_info
        if info.state == DrawdownState.CRITICAL:
            # 停止開新倉

        # 停損計算
        stop = re.calc_stop_loss(entry_price=850, atr=12.5, method="atr", atr_mult=2.0)
    """

    def __init__(
        self,
        initial_capital:  float = 1_000_000,
        warning_dd_pct:   float = 0.10,   # 回撤 10% 發出警告
        critical_dd_pct:  float = 0.20,   # 回撤 20% 停止交易
        atr_volatile_pct: float = 0.80,   # ATR 高於此百分位 → 高波動
    ):
        self.initial_capital   = initial_capital
        self.warning_dd_pct    = warning_dd_pct
        self.critical_dd_pct   = critical_dd_pct
        self.atr_volatile_pct  = atr_volatile_pct

        self._peak_equity      = initial_capital
        self._current_equity   = initial_capital
        self._max_drawdown     = 0.0
        self._dd_state         = DrawdownState.NORMAL

    # ── 盤態偵測 ──────────────────────────────────────────────────────────

    def detect_regime(self, df: pd.DataFrame) -> RegimeResult:
        """
        偵測市場盤態。

        df 必須包含 close（和 volume 欄位可選）。
        若已由 FeatureEngine 計算，則可直接帶入含特徵的 DataFrame；
        否則此函式會自行計算所需指標。
        """
        if len(df) < 20:
            return self._unknown_regime(df["close"].iloc[-1] if len(df) > 0 else 0)

        close = pd.to_numeric(df["close"], errors="coerce")

        # 計算所需均線（若 FeatureEngine 已算過直接取用）
        ma5   = df["ma5"].iloc[-1]   if "ma5"   in df.columns else close.rolling(5).mean().iloc[-1]
        ma20  = df["ma20"].iloc[-1]  if "ma20"  in df.columns else close.rolling(20).mean().iloc[-1]
        ma60  = df["ma60"].iloc[-1]  if "ma60"  in df.columns else close.rolling(60).mean().iloc[-1]
        ma200 = None
        if len(df) >= 200:
            ma200 = df["ma200"].iloc[-1] if "ma200" in df.columns else close.rolling(200).mean().iloc[-1]

        atr14 = df["atr14"].iloc[-1] if "atr14" in df.columns else self._calc_atr(df, 14)
        last_close = float(close.iloc[-1])

        # ── 波動度評估 ────────────────────────────────────────────────────
        atr_series = df["atr14"] if "atr14" in df.columns else self._calc_atr_series(df, 14)
        atr_pct = atr14 / last_close if last_close > 0 else None
        atr_percentile = None
        if atr_series is not None and len(atr_series.dropna()) >= 20:
            atr_percentile = float(pd.Series(atr_series.dropna()).rank(pct=True).iloc[-1])
            if atr_percentile >= self.atr_volatile_pct:
                rec = REGIME_STRATEGY_MAP[MarketRegime.VOLATILE]
                return RegimeResult(
                    regime=MarketRegime.VOLATILE,
                    description=REGIME_DESC[MarketRegime.VOLATILE],
                    confidence=round(atr_percentile, 2),
                    close=last_close, ma5=ma5, ma20=ma20, ma200=ma200,
                    atr14=atr14, atr_pct=round(atr_pct, 4) if atr_pct else None,
                    strategies=rec["strategies"],
                    max_long_pct=rec["max_long_pct"],
                    stop_loss=rec["stop_loss"],
                    tip=rec["tip"],
                    extra={"atr_percentile": atr_percentile},
                )

        # ── 主趨勢判斷 ───────────────────────────────────────────────────
        bull_score = 0
        bear_score = 0

        if not np.isnan(ma5) and not np.isnan(ma20):
            if ma5 > ma20:
                bull_score += 2
            else:
                bear_score += 2

        if ma200 is not None and not np.isnan(ma200):
            if last_close > ma200:
                bull_score += 2
            else:
                bear_score += 2

        if not np.isnan(ma20) and not np.isnan(ma60):
            if ma20 > ma60:
                bull_score += 1
            else:
                bear_score += 1

        total = bull_score + bear_score
        confidence = max(bull_score, bear_score) / total if total > 0 else 0.5

        if bull_score > bear_score:
            regime = MarketRegime.BULL
        elif bear_score > bull_score:
            regime = MarketRegime.BEAR
        else:
            regime = MarketRegime.SIDEWAYS

        rec = REGIME_STRATEGY_MAP[regime]
        return RegimeResult(
            regime=regime,
            description=REGIME_DESC[regime],
            confidence=round(confidence, 2),
            close=last_close, ma5=ma5, ma20=ma20, ma200=ma200,
            atr14=atr14,
            atr_pct=round(atr_pct, 4) if atr_pct else None,
            strategies=rec["strategies"],
            max_long_pct=rec["max_long_pct"],
            stop_loss=rec["stop_loss"],
            tip=rec["tip"],
            extra={"bull_score": bull_score, "bear_score": bear_score},
        )

    # ── 回撤追蹤 ──────────────────────────────────────────────────────────

    def update_equity(self, current_equity: float) -> DrawdownInfo:
        """
        更新當前淨值，自動計算回撤並判斷狀態。
        每個 bar（或每日收盤）呼叫一次。
        """
        self._current_equity = current_equity

        # 更新最高淨值
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity

        # 計算回撤
        dd = (self._peak_equity - current_equity) / self._peak_equity
        self._max_drawdown = max(self._max_drawdown, dd)

        # 狀態判斷
        if dd >= self.critical_dd_pct:
            self._dd_state = DrawdownState.CRITICAL
        elif dd >= self.warning_dd_pct:
            self._dd_state = DrawdownState.WARNING
        else:
            if self._dd_state != DrawdownState.CRITICAL:  # CRITICAL 需手動解除
                self._dd_state = DrawdownState.NORMAL

        if dd >= self.critical_dd_pct:
            logger.warning(f"[RiskEngine] 回撤 {dd*100:.1f}% 超過停損線，應停止開新倉")

        return self.drawdown_info

    @property
    def drawdown_info(self) -> DrawdownInfo:
        current = self._current_equity
        dd = (self._peak_equity - current) / self._peak_equity if self._peak_equity > 0 else 0
        return DrawdownInfo(
            peak_equity=self._peak_equity,
            current_equity=current,
            drawdown_pct=round(dd, 4),
            max_drawdown=round(self._max_drawdown, 4),
            state=self._dd_state,
            warning_pct=self.warning_dd_pct,
            critical_pct=self.critical_dd_pct,
        )

    def reset_drawdown(self) -> None:
        """重置回撤追蹤（策略重啟時呼叫）"""
        self._peak_equity    = self._current_equity
        self._max_drawdown   = 0.0
        self._dd_state       = DrawdownState.NORMAL

    @property
    def can_open_position(self) -> bool:
        """回傳是否允許開新倉"""
        return self._dd_state not in (DrawdownState.CRITICAL, DrawdownState.HALTED)

    # ── 停損計算 ──────────────────────────────────────────────────────────

    def calc_stop_loss(
        self,
        entry_price: float,
        side:        str   = "buy",    # "buy" 或 "sell"
        method:      str   = "fixed",  # "fixed" 或 "atr"
        fixed_pct:   float = 0.08,     # 固定停損幅度 8%
        atr:         float = 0.0,      # ATR14 值（method="atr" 時使用）
        atr_mult:    float = 2.0,      # ATR 倍數（通常 1.5~3）
    ) -> float:
        """
        計算停損價格。

        method="fixed"：停損 = 進場價 × (1 ∓ fixed_pct)
        method="atr"  ：停損 = 進場價 ∓ atr × atr_mult
        """
        if method == "atr" and atr > 0:
            loss = atr * atr_mult
        else:
            loss = entry_price * fixed_pct

        if side == "buy":
            return round(entry_price - loss, 2)
        else:
            return round(entry_price + loss, 2)

    def calc_take_profit(
        self,
        entry_price: float,
        side:        str   = "buy",
        rr_ratio:    float = 2.0,    # 獲利 / 風險 比（預設 2:1）
        stop_price:  Optional[float] = None,
        fixed_pct:   float = 0.10,
    ) -> float:
        """
        計算停利價格。
        若提供 stop_price 則依風報比計算；否則使用 fixed_pct。
        """
        if stop_price is not None:
            risk = abs(entry_price - stop_price)
            reward = risk * rr_ratio
        else:
            reward = entry_price * fixed_pct

        if side == "buy":
            return round(entry_price + reward, 2)
        else:
            return round(entry_price - reward, 2)

    # ── VaR 估算 ──────────────────────────────────────────────────────────

    def calc_var(
        self,
        returns:     pd.Series,
        confidence:  float = 0.95,
        method:      str   = "historical",  # "historical" 或 "parametric"
        portfolio_value: float = 1_000_000,
    ) -> dict:
        """
        計算單日 VaR（Value at Risk）。

        method="historical"  : 歷史模擬法（最準確，需 > 250 筆）
        method="parametric"  : 參數法（常態分佈假設）

        回傳 dict: var_pct, var_amount, cvar_pct, cvar_amount
        """
        clean = returns.dropna()
        if len(clean) < 20:
            return {"error": "資料不足（< 20 筆）"}

        alpha = 1 - confidence

        if method == "historical":
            var_pct  = float(clean.quantile(alpha))
            cvar_pct = float(clean[clean <= var_pct].mean()) if (clean <= var_pct).any() else var_pct
        else:
            mu  = clean.mean()
            sig = clean.std()
            from scipy.stats import norm
            var_pct  = float(norm.ppf(alpha, loc=mu, scale=sig))
            # CVaR（Expected Shortfall）= -E[r | r < VaR]
            cvar_pct = float(mu - sig * norm.pdf(norm.ppf(alpha)) / alpha)

        return {
            "method":       method,
            "confidence":   confidence,
            "var_pct":      round(var_pct, 4),
            "var_amount":   round(abs(var_pct) * portfolio_value, 0),
            "cvar_pct":     round(cvar_pct, 4),
            "cvar_amount":  round(abs(cvar_pct) * portfolio_value, 0),
            "n_samples":    len(clean),
        }

    # ── 輔助方法 ──────────────────────────────────────────────────────────

    def _unknown_regime(self, close: float) -> RegimeResult:
        rec = REGIME_STRATEGY_MAP[MarketRegime.UNKNOWN]
        return RegimeResult(
            regime=MarketRegime.UNKNOWN,
            description=REGIME_DESC[MarketRegime.UNKNOWN],
            confidence=0.0,
            close=close, ma5=close, ma20=close, ma200=None,
            atr14=None, atr_pct=None,
            strategies=rec["strategies"],
            max_long_pct=rec["max_long_pct"],
            stop_loss=rec["stop_loss"],
            tip=rec["tip"],
        )

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
        """計算最新 ATR 值"""
        s = RiskEngine._calc_atr_series(df, period)
        return float(s.iloc[-1]) if s is not None and len(s) > 0 else 0.0

    @staticmethod
    def _calc_atr_series(df: pd.DataFrame, period: int = 14) -> Optional[pd.Series]:
        """計算 ATR Series"""
        if not {"high", "low", "close"}.issubset(df.columns):
            return None
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / period, adjust=False).mean()


# ── Mock 資料 + 獨立測試 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from quant.feature_engine import FeatureEngine, _generate_mock_ohlcv

    mock_df = _generate_mock_ohlcv(300)
    fe  = FeatureEngine(mock_df)
    df  = fe.compute_all()

    re = RiskEngine(initial_capital=1_000_000)

    print("=== 盤態偵測 ===")
    regime = re.detect_regime(df)
    print(f"盤態: {regime.regime.value} ({regime.description})  信心: {regime.confidence}")
    print(f"MA5={regime.ma5:.2f}  MA20={regime.ma20:.2f}  MA200={regime.ma200}")
    print(f"建議策略: {regime.strategies}")
    print(f"最大多頭倉位: {regime.max_long_pct*100:.0f}%  停損: {regime.stop_loss*100:.0f}%")
    print(f"操作提示: {regime.tip}")

    print("\n=== 回撤追蹤 ===")
    equities = [1_000_000, 1_050_000, 1_080_000, 980_000, 870_000, 820_000]
    for eq in equities:
        info = re.update_equity(eq)
        print(f"淨值={eq:>10,.0f}  回撤={info.drawdown_pct*100:+.1f}%  狀態={info.state.value}")

    print("\n=== 停損 / 停利 計算 ===")
    entry = 850.0
    atr   = 12.5
    stop  = re.calc_stop_loss(entry, method="atr", atr=atr, atr_mult=2.0)
    tp    = re.calc_take_profit(entry, stop_price=stop, rr_ratio=2.0)
    print(f"進場價={entry}  ATR={atr}")
    print(f"停損價={stop}  停利價={tp}  風報比=2:1")

    print("\n=== VaR 估算 ===")
    returns = df["ret_1d"].dropna()
    var = re.calc_var(returns, confidence=0.95, method="historical", portfolio_value=1_000_000)
    print(f"歷史 VaR(95%): {var['var_pct']*100:.2f}%  金額: NT${var['var_amount']:,.0f}")
    print(f"CVaR(95%):    {var['cvar_pct']*100:.2f}%  金額: NT${var['cvar_amount']:,.0f}")
