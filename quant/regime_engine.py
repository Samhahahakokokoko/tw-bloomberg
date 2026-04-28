"""
regime_engine.py — Market Regime v2（機構級盤態偵測）

輸入：大盤指數時序（或個股 OHLCV）+ 波動率 proxy + MA20/MA60
輸出：bull / bear / sideways（+ 子狀態）

偵測邏輯（多層確認）：
  Layer 1 趨勢：  close vs MA20 / MA60
  Layer 2 動量：  20日報酬率正負
  Layer 3 波動：  20日年化波動率 vs 歷史中位數
  Layer 4 成交量：量能是否放大（可選）

盤態 → 策略分配：
  bull     → momentum × 1.5, breakout × 1.3, defensive × 0.5
  bear     → defensive × 2.0, value × 1.5, momentum × 0.4
  sideways → chip × 1.3, mean_reversion × 1.5, momentum × 0.8
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    BULL      = "bull"
    BEAR      = "bear"
    SIDEWAYS  = "sideways"
    VOLATILE  = "volatile"   # 高波動，跨越 bull/bear
    UNKNOWN   = "unknown"


@dataclass
class RegimeV2:
    """盤態偵測結果（v2）"""
    regime:          Regime
    sub_label:       str           # 例："strong_bull" / "early_bear"
    confidence:      float         # 0~1

    # 技術指標快照
    close:           float
    ma20:            float
    ma60:            float
    vol_20d:         float         # 20日年化波動率
    vol_median:      float         # 歷史中位數波動率
    ret_20d:         float         # 20日報酬率

    # 策略分配
    alpha_weights:   dict[str, float]   # {alpha_name: weight_multiplier}
    position_scale:  float              # 整體倉位乘數 0~1
    note:            str

    def to_dict(self) -> dict:
        return {
            "regime":         self.regime.value,
            "sub_label":      self.sub_label,
            "confidence":     round(self.confidence, 3),
            "close":          round(self.close, 2),
            "ma20":           round(self.ma20, 2),
            "ma60":           round(self.ma60, 2),
            "vol_20d_ann":    round(self.vol_20d * 100, 2),
            "vol_median_ann": round(self.vol_median * 100, 2),
            "ret_20d_pct":    round(self.ret_20d * 100, 2),
            "alpha_weights":  {k: round(v, 3) for k, v in self.alpha_weights.items()},
            "position_scale": round(self.position_scale, 3),
            "note":           self.note,
        }


# 策略分配表（每種盤態的 alpha 乘數）
_ALPHA_WEIGHT_TABLE: dict[Regime, dict[str, float]] = {
    Regime.BULL: {
        "momentum": 1.50, "breakout": 1.30, "chip": 1.10,
        "value":    0.70, "defensive": 0.50,
    },
    Regime.BEAR: {
        "momentum": 0.40, "breakout": 0.35, "chip": 0.70,
        "value":    1.50, "defensive": 2.00,
    },
    Regime.SIDEWAYS: {
        "momentum": 0.80, "breakout": 0.80, "chip": 1.30,
        "value":    1.20, "defensive": 1.00, "mean_reversion": 1.50,
    },
    Regime.VOLATILE: {
        "momentum": 0.50, "breakout": 0.45, "chip": 0.70,
        "value":    1.30, "defensive": 1.80,
    },
    Regime.UNKNOWN: {
        "momentum": 0.70, "breakout": 0.70, "chip": 0.80,
        "value":    1.20, "defensive": 1.50,
    },
}

_POSITION_SCALE_TABLE: dict[Regime, float] = {
    Regime.BULL:     1.00,
    Regime.BEAR:     0.45,
    Regime.SIDEWAYS: 0.75,
    Regime.VOLATILE: 0.40,
    Regime.UNKNOWN:  0.50,
}


class RegimeEngine:
    """
    Market Regime v2 偵測引擎。

    使用方式：
        engine = RegimeEngine()
        result = engine.detect(price_df)   # df 含 close，可含 volume

    price_df 可以是大盤指數序列（OHLCV），也可以是個股資料，
    用個股資料時 regime 代表該股票的個別狀態。
    """

    def __init__(
        self,
        vol_multiplier: float = 1.5,   # 高波動閾值：20d vol > median × 倍數
        ma_short: int = 20,
        ma_long:  int = 60,
        ret_window: int = 20,
    ):
        self.vol_mult   = vol_multiplier
        self.ma_short   = ma_short
        self.ma_long    = ma_long
        self.ret_window = ret_window

    def detect(self, df: pd.DataFrame) -> RegimeV2:
        """
        偵測盤態。
        df 必須含 close 欄位，至少 ma_long + ret_window 筆。
        """
        df = df.copy().reset_index(drop=True)
        n  = len(df)
        min_required = self.ma_long + self.ret_window + 5

        if n < min_required:
            return self._unknown(df.iloc[-1]["close"] if n else 0.0)

        close = df["close"].astype(float)

        # ── 技術指標 ─────────────────────────────────────────────────────
        ma20 = float(close.rolling(self.ma_short, min_periods=1).mean().iloc[-1])
        ma60 = float(close.rolling(self.ma_long,  min_periods=1).mean().iloc[-1])
        last = float(close.iloc[-1])

        # 20 日報酬率
        ret_20d = float((close.iloc[-1] / close.iloc[max(-1 - self.ret_window, -n)]) - 1)

        # 20 日年化波動率
        daily_ret = close.pct_change().dropna()
        vol_20d   = float(daily_ret.iloc[-self.ret_window:].std() * np.sqrt(252))
        vol_med   = float(daily_ret.rolling(60, min_periods=20).std().iloc[-1] * np.sqrt(252))
        if np.isnan(vol_med) or vol_med <= 0:
            vol_med = vol_20d

        # ── Layer 1：趨勢判定（MA） ──────────────────────────────────────
        above_ma20 = last > ma20
        above_ma60 = last > ma60
        ma20_up    = ma20 > float(close.rolling(self.ma_short).mean().iloc[-5]) if n > self.ma_short + 5 else True

        # ── Layer 2：動量 ────────────────────────────────────────────────
        positive_momentum = ret_20d > 0

        # ── Layer 3：波動 ────────────────────────────────────────────────
        high_vol = vol_20d > vol_med * self.vol_mult

        # ── 盤態決定（多層確認）──────────────────────────────────────────
        confidence = 0.5

        if high_vol:
            regime    = Regime.VOLATILE
            sub_label = "high_volatility"
            confidence = 0.60 + min(0.30, (vol_20d / vol_med - self.vol_mult) * 0.2)
            note = f"20日波動率={vol_20d*100:.1f}%，超過中位數{self.vol_mult}倍"

        elif above_ma20 and above_ma60 and positive_momentum:
            regime    = Regime.BULL
            # 子狀態
            if ret_20d > 0.05 and ma20_up:
                sub_label  = "strong_bull"
                confidence = 0.85
            else:
                sub_label  = "mild_bull"
                confidence = 0.70
            note = f"站上 MA20({ma20:.0f}) MA60({ma60:.0f})，20日漲{ret_20d*100:.1f}%"

        elif not above_ma20 and not above_ma60 and not positive_momentum:
            regime    = Regime.BEAR
            if ret_20d < -0.08:
                sub_label  = "strong_bear"
                confidence = 0.85
            else:
                sub_label  = "mild_bear"
                confidence = 0.70
            note = f"跌破 MA20({ma20:.0f}) MA60({ma60:.0f})，20日跌{abs(ret_20d)*100:.1f}%"

        else:
            # 介於中間 → 盤整
            regime    = Regime.SIDEWAYS
            sub_label = "consolidation"
            confidence = 0.55
            note = f"MA20={ma20:.0f} MA60={ma60:.0f} 無明確趨勢"

        alpha_weights = dict(_ALPHA_WEIGHT_TABLE.get(regime, {}))
        position_scale = _POSITION_SCALE_TABLE.get(regime, 0.5)

        return RegimeV2(
            regime=regime,
            sub_label=sub_label,
            confidence=round(confidence, 3),
            close=round(last, 2),
            ma20=round(ma20, 2),
            ma60=round(ma60, 2),
            vol_20d=round(vol_20d, 4),
            vol_median=round(vol_med, 4),
            ret_20d=round(ret_20d, 4),
            alpha_weights=alpha_weights,
            position_scale=round(position_scale, 3),
            note=note,
        )

    def _unknown(self, close: float) -> RegimeV2:
        return RegimeV2(
            regime=Regime.UNKNOWN, sub_label="insufficient_data",
            confidence=0.0, close=close, ma20=close, ma60=close,
            vol_20d=0.0, vol_median=0.0, ret_20d=0.0,
            alpha_weights=dict(_ALPHA_WEIGHT_TABLE[Regime.UNKNOWN]),
            position_scale=0.5,
            note="資料不足，無法偵測盤態",
        )

    def detect_from_series(self, closes: list[float]) -> RegimeV2:
        """便利方法：直接傳入收盤價列表"""
        df = pd.DataFrame({"close": closes})
        return self.detect(df)


_global_regime: Optional[RegimeEngine] = None

def get_regime_engine() -> RegimeEngine:
    global _global_regime
    if _global_regime is None:
        _global_regime = RegimeEngine()
    return _global_regime
