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
    PANIC     = "panic"      # 恐慌：單日大跌 + 爆量 + 大量跌停
    EUPHORIA  = "euphoria"   # 狂歡：連漲 + 量爆增 + 大量漲停
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
    Regime.PANIC: {
        "momentum": 0.20, "breakout": 0.20, "chip": 0.50,
        "value":    1.80, "defensive": 2.50, "mean_reversion": 1.20,
    },
    Regime.EUPHORIA: {
        "momentum": 0.60, "breakout": 0.50, "chip": 0.80,
        "value":    1.00, "defensive": 1.50,
    },
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
    Regime.PANIC:    0.15,   # 恐慌：極小倉位，等待反彈
    Regime.EUPHORIA: 0.30,   # 狂歡：降低倉位至 30%，避免追高
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


# ═══════════════════════════════════════════════════════════════════
#  EnhancedRegimeEngine — 五態偵測（含 panic / euphoria）
#  額外輸入：daily_change / foreign_futures_net / limit_up|down_count
#            / tsmc_trend / volume
# ═══════════════════════════════════════════════════════════════════

@dataclass
class EnhancedRegimeResult:
    """五態盤態偵測結果"""
    regime:          Regime
    sub_label:       str
    confidence:      float
    close:           float
    ma20:            float
    ma60:            float
    vol_20d:         float
    ret_20d:         float
    daily_change_pct: float
    alpha_weights:   dict[str, float]
    position_scale:  float
    note:            str

    def to_dict(self) -> dict:
        return {
            "regime":           self.regime.value,
            "sub_label":        self.sub_label,
            "confidence":       round(self.confidence, 3),
            "close":            round(self.close, 2),
            "ma20":             round(self.ma20, 2),
            "ma60":             round(self.ma60, 2),
            "vol_20d_ann":      round(self.vol_20d * 100, 2),
            "ret_20d_pct":      round(self.ret_20d * 100, 2),
            "daily_change_pct": round(self.daily_change_pct * 100, 2),
            "alpha_weights":    {k: round(v, 3) for k, v in self.alpha_weights.items()},
            "position_scale":   round(self.position_scale, 3),
            "note":             self.note,
        }


class EnhancedRegimeEngine(RegimeEngine):
    """
    五態市場盤態偵測引擎（bull / bear / sideways / panic / euphoria）。

    在 RegimeEngine v2 基礎上，額外偵測 panic 與 euphoria：

    Panic 條件（任一滿足）：
      - 單日跌幅 > 3%
      - 跌停家數 > limit_down_threshold（預設 30 家）
      - 同時成交量 > volume_surge_mult × 20日均量

    Euphoria 條件（任一滿足）：
      - 連漲天數 ≥ euphoria_streak（預設 5 天）且成交量爆增
      - 漲停家數 > limit_up_threshold（預設 50 家）
      - 20日報酬 > euphoria_ret_pct（預設 15%）+ 量爆增
    """

    def __init__(
        self,
        vol_multiplier:        float = 1.5,
        ma_short:              int   = 20,
        ma_long:               int   = 60,
        ret_window:            int   = 20,
        # 恐慌閾值
        panic_daily_drop:      float = 0.03,   # 單日跌幅 > 3%
        panic_limit_down:      int   = 30,     # 跌停家數 > 30
        panic_vol_surge:       float = 2.0,    # 成交量 > 2× 均量
        # 狂歡閾值
        euphoria_streak:       int   = 5,      # 連漲天數
        euphoria_limit_up:     int   = 50,     # 漲停家數 > 50
        euphoria_ret_pct:      float = 0.15,   # 20日漲幅 > 15%
        euphoria_vol_surge:    float = 1.8,    # 成交量 > 1.8× 均量
    ):
        super().__init__(vol_multiplier, ma_short, ma_long, ret_window)
        self.panic_daily_drop   = panic_daily_drop
        self.panic_limit_down   = panic_limit_down
        self.panic_vol_surge    = panic_vol_surge
        self.euphoria_streak    = euphoria_streak
        self.euphoria_limit_up  = euphoria_limit_up
        self.euphoria_ret_pct   = euphoria_ret_pct
        self.euphoria_vol_surge = euphoria_vol_surge

    def detect_enhanced(
        self,
        df:                  pd.DataFrame,
        daily_change_pct:    float = 0.0,    # 今日大盤漲跌幅（如 -0.035 = -3.5%）
        foreign_futures_net: float = 0.0,    # 外資期貨淨多口數（正=多頭）
        limit_up_count:      int   = 0,      # 今日漲停家數
        limit_down_count:    int   = 0,      # 今日跌停家數
        tsmc_trend:          str   = "neutral",  # "up"/"down"/"neutral"
        volume_today:        float = 0.0,    # 今日大盤成交量（億）
    ) -> EnhancedRegimeResult:
        """
        五態偵測主函式。
        先判斷 panic / euphoria（外部訊號），再由 RegimeEngine 判斷基礎盤態。
        """
        # ── 計算基礎技術指標（共用）──────────────────────────────────────
        df = df.copy().reset_index(drop=True)
        n  = len(df)

        close    = df["close"].astype(float)
        last     = float(close.iloc[-1]) if n > 0 else 0.0
        ma20     = float(close.rolling(self.ma_short, min_periods=1).mean().iloc[-1]) if n > 0 else last
        ma60     = float(close.rolling(self.ma_long,  min_periods=1).mean().iloc[-1]) if n > 0 else last
        ret_20d  = float((close.iloc[-1] / close.iloc[max(-1-self.ret_window, -n)]) - 1) if n > 1 else 0.0
        daily_ret = close.pct_change().dropna()
        vol_20d   = float(daily_ret.iloc[-self.ret_window:].std() * np.sqrt(252)) if len(daily_ret) >= 5 else 0.0

        # 計算 20 日均量（用於量能比較）
        vol_avg_20 = 0.0
        if "volume" in df.columns and n >= 20:
            vol_avg_20 = float(df["volume"].iloc[-20:].mean())

        # ── Panic 偵測（優先級最高）────────────────────────────────────
        is_panic = False
        panic_reasons = []

        if daily_change_pct <= -self.panic_daily_drop:
            is_panic = True
            panic_reasons.append(f"單日跌幅 {daily_change_pct*100:.1f}%")

        if limit_down_count >= self.panic_limit_down:
            is_panic = True
            panic_reasons.append(f"跌停 {limit_down_count} 家")

        if is_panic and vol_avg_20 > 0 and volume_today > 0:
            vol_ratio = volume_today / (vol_avg_20 / 1e8) if vol_avg_20 > 1e8 else 1.0
            if vol_ratio > self.panic_vol_surge:
                panic_reasons.append(f"量能 {vol_ratio:.1f}× 均量")

        if is_panic:
            regime = Regime.PANIC
            note   = "恐慌：" + "、".join(panic_reasons)
            return self._make_result(regime, "fear_selling", 0.90,
                                     last, ma20, ma60, vol_20d, ret_20d,
                                     daily_change_pct, note)

        # ── Euphoria 偵測 ──────────────────────────────────────────────
        is_euphoria    = False
        euphoria_reasons = []

        # 漲停家數觸發
        if limit_up_count >= self.euphoria_limit_up:
            is_euphoria = True
            euphoria_reasons.append(f"漲停 {limit_up_count} 家")

        # 20日大漲 + 量爆增
        if ret_20d >= self.euphoria_ret_pct:
            vol_ratio = 1.0
            if vol_avg_20 > 0 and volume_today > 0:
                vol_ratio = volume_today / (vol_avg_20 / 1e8) if vol_avg_20 > 1e8 else 1.0
            if vol_ratio >= self.euphoria_vol_surge:
                is_euphoria = True
                euphoria_reasons.append(f"20日漲{ret_20d*100:.1f}%+量{vol_ratio:.1f}×")

        # 連漲天數（滾動計算）
        if n >= self.euphoria_streak:
            streak = 0
            for i in range(1, min(self.euphoria_streak + 2, n)):
                if float(close.iloc[-i]) < float(close.iloc[-(i+1)]):
                    break
                streak += 1
            if streak >= self.euphoria_streak:
                is_euphoria = True
                euphoria_reasons.append(f"連漲 {streak} 天")

        # 台積電趨勢強化（領頭羊信號）
        if tsmc_trend == "up" and ret_20d > 0.08:
            is_euphoria = True
            euphoria_reasons.append("台積電帶頭")

        if is_euphoria:
            regime = Regime.EUPHORIA
            note   = "狂歡：" + "、".join(euphoria_reasons)
            return self._make_result(regime, "fomo_chase", 0.80,
                                     last, ma20, ma60, vol_20d, ret_20d,
                                     daily_change_pct, note)

        # ── 基礎盤態（外資期貨加權）──────────────────────────────────
        base = self.detect(df)

        # 外資期貨淨多 → 偏多信號（調整信心）
        conf_adj = 0.0
        if foreign_futures_net > 5000:
            conf_adj = +0.05
        elif foreign_futures_net < -5000:
            conf_adj = -0.05

        # tsmc_trend 與大盤趨勢一致 → 提升信心
        if tsmc_trend == "up" and base.regime == Regime.BULL:
            conf_adj += 0.03
        elif tsmc_trend == "down" and base.regime == Regime.BEAR:
            conf_adj += 0.03

        adj_conf = max(0.0, min(1.0, base.confidence + conf_adj))
        note     = base.note
        if conf_adj != 0:
            note += f"  (外資期貨{foreign_futures_net:+.0f}口, 台積電{tsmc_trend})"

        return EnhancedRegimeResult(
            regime=base.regime,
            sub_label=base.sub_label,
            confidence=round(adj_conf, 3),
            close=base.close, ma20=base.ma20, ma60=base.ma60,
            vol_20d=base.vol_20d, ret_20d=base.ret_20d,
            daily_change_pct=daily_change_pct,
            alpha_weights=base.alpha_weights,
            position_scale=base.position_scale,
            note=note,
        )

    def _make_result(
        self, regime: Regime, sub_label: str, confidence: float,
        close: float, ma20: float, ma60: float,
        vol_20d: float, ret_20d: float, daily_change_pct: float,
        note: str,
    ) -> EnhancedRegimeResult:
        return EnhancedRegimeResult(
            regime=regime, sub_label=sub_label, confidence=confidence,
            close=round(close, 2), ma20=round(ma20, 2), ma60=round(ma60, 2),
            vol_20d=round(vol_20d, 4), ret_20d=round(ret_20d, 4),
            daily_change_pct=round(daily_change_pct, 4),
            alpha_weights=dict(_ALPHA_WEIGHT_TABLE.get(regime, {})),
            position_scale=_POSITION_SCALE_TABLE.get(regime, 0.5),
            note=note,
        )


_global_enhanced: Optional[EnhancedRegimeEngine] = None

def get_enhanced_regime_engine() -> EnhancedRegimeEngine:
    global _global_enhanced
    if _global_enhanced is None:
        _global_enhanced = EnhancedRegimeEngine()
    return _global_enhanced


# ── Mock + 獨立測試 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    rng   = np.random.default_rng(42)
    n     = 150
    close = 100 * np.cumprod(1 + rng.normal(0.0003, 0.012, n))
    df    = pd.DataFrame({"close": close})

    engine = EnhancedRegimeEngine()

    print("=== Normal market ===")
    r = engine.detect_enhanced(df, daily_change_pct=-0.01, foreign_futures_net=3000)
    print(r.to_dict())

    print("\n=== Panic ===")
    r = engine.detect_enhanced(df, daily_change_pct=-0.045, limit_down_count=55)
    print(r.to_dict())

    print("\n=== Euphoria ===")
    r = engine.detect_enhanced(df, daily_change_pct=0.02, limit_up_count=80,
                               tsmc_trend="up")
    print(r.to_dict())
