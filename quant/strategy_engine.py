"""
strategy_engine.py — 台股 AI 多策略選股引擎

三大策略：
  A. 動能策略（momentum）  — 追蹤趨勢延續、外資動向、量能爆發
  B. 價值存股（value）     — 高殖利率、低 PE、EPS 穩定
  C. 籌碼追蹤（chip）      — 外資/投信/自營三大法人合力評分

複合評分：
  composite = A × w_mom + B × w_val + C × w_chip
  w_mom, w_val, w_chip 根據 MarketRegime 動態調整

信心指數（0~100）：
  confidence = backtest_score × 0.30
             + model_pred_score × 0.40
             + signal_strength × 0.30

風險等級：
  risk_score = volatility × 0.5 + max_drawdown × 0.5
  < 0.20 → 低　　0.20~0.40 → 中　　> 0.40 → 高

行動建議：
  confidence ≥ 75 → 強力買進
  confidence ≥ 60 → 買進
  confidence ≥ 45 → 觀察
  confidence ≥ 30 → 減碼
             < 30 → 賣出
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

class Action(str, Enum):
    STRONG_BUY = "強力買進"
    BUY        = "買進"
    WATCH      = "觀察"
    REDUCE     = "減碼"
    SELL       = "賣出"

class RiskLevel(str, Enum):
    LOW    = "低"
    MEDIUM = "中"
    HIGH   = "高"

# 盤態 → 策略權重映射
REGIME_WEIGHTS: dict[str, dict] = {
    "bull":     {"momentum": 0.50, "value": 0.20, "chip": 0.30},
    "bear":     {"momentum": 0.10, "value": 0.50, "chip": 0.40},
    "sideways": {"momentum": 0.25, "value": 0.40, "chip": 0.35},
    "volatile": {"momentum": 0.15, "value": 0.45, "chip": 0.40},
    "unknown":  {"momentum": 0.33, "value": 0.34, "chip": 0.33},
}

# 行動閾值
ACTION_THRESHOLDS = [
    (75, Action.STRONG_BUY),
    (60, Action.BUY),
    (45, Action.WATCH),
    (30, Action.REDUCE),
    (0,  Action.SELL),
]

# ── 資料類別 ─────────────────────────────────────────────────────────────────

@dataclass
class StrategySignal:
    """單股策略輸出結果（JSON-friendly）"""
    stock_id:     str
    name:         str
    action:       Action
    confidence:   float           # 0~100
    reasons:      list[str]
    target_price: Optional[float]
    stop_loss:    Optional[float]
    holding_days: int
    risk_level:   RiskLevel
    strategy:     str             # momentum / value / chip / composite
    # 子分數
    momentum_score: float = 0.0
    value_score:    float = 0.0
    chip_score:     float = 0.0
    composite_score: float = 0.0
    risk_score:     float = 0.0

    def to_dict(self) -> dict:
        return {
            "stock_id":       self.stock_id,
            "name":           self.name,
            "action":         self.action.value,
            "confidence":     round(self.confidence, 1),
            "reasons":        self.reasons,
            "target_price":   self.target_price,
            "stop_loss":      self.stop_loss,
            "holding_days":   self.holding_days,
            "risk_level":     self.risk_level.value,
            "strategy":       self.strategy,
            "scores": {
                "momentum":   round(self.momentum_score, 1),
                "value":      round(self.value_score, 1),
                "chip":       round(self.chip_score, 1),
                "composite":  round(self.composite_score, 1),
                "risk":       round(self.risk_score, 3),
            },
        }

    def to_line_text(self) -> str:
        """LINE Bot 用的訊息格式"""
        emoji = {"強力買進": "🔥", "買進": "▲", "觀察": "◆", "減碼": "▽", "賣出": "🔴"}
        e = emoji.get(self.action.value, "")
        lines = [
            f"{e} {self.stock_id} {self.name}",
            f"建議：{self.action.value}  信心：{self.confidence:.0f}",
            f"目標：{self.target_price:.0f}  停損：{self.stop_loss:.0f}  風險：{self.risk_level.value}",
            f"理由：{'、'.join(self.reasons[:3])}",
        ]
        return "\n".join(lines)


# ── 策略引擎 ─────────────────────────────────────────────────────────────────

class StrategyEngine:
    """
    多策略選股引擎。

    使用方式：
        engine = StrategyEngine()
        signal = engine.evaluate(data, strategy="composite", regime="bull")

    data 欄位說明（所有欄位均可缺失，會使用預設值）：
      momentum_20d      float   20日報酬率比（1.08 = +8%）
      foreign_buy_days  int     外資連買天數（負=連賣）
      volume_ratio      float   今日量 / 5日均量
      dividend_yield    float   殖利率（%）
      pe_ratio          float   本益比
      eps_stability     float   EPS 穩定度 0~1
      foreign_net       float   外資淨買張數
      trust_net         float   投信淨買張數
      dealer_net        float   自營商淨買張數
      chip_concentration float  籌碼集中度 0~100
      volatility        float   日報酬率標準差（如 0.015 = 1.5%）
      max_drawdown      float   最大回撤（如 0.12 = 12%）
      close             float   收盤價
      atr14             float   ATR14（用於計算停損）
      backtest_sharpe   float   回測夏普值（可選）
      pred_ret          float   模型預測報酬率（可選）
      stock_id          str
      name              str
    """

    # ── 策略 A：動能 ──────────────────────────────────────────────────────

    def momentum_strategy(self, data: dict) -> tuple[float, list[str]]:
        """
        動能策略評分：
          momentum_20d  35 分
          foreign_buy   35 分
          volume_ratio  30 分
        """
        score   = 0.0
        reasons = []

        # 20 日動能
        mom = float(data.get("momentum_20d", 1.0))
        mom_pct = (mom - 1) * 100
        if mom > 1.10:
            score += 35
            reasons.append(f"20日強勢動能+{mom_pct:.1f}%")
        elif mom > 1.05:
            score += 28
            reasons.append(f"20日動能+{mom_pct:.1f}%")
        elif mom > 1.02:
            score += 15
        elif mom < 0.97:
            score -= 10  # 趨勢弱

        # 外資連買
        fb = int(data.get("foreign_buy_days", 0))
        if fb >= 7:
            score += 35
            reasons.append(f"外資連買{fb}日（強烈加碼）")
        elif fb >= 5:
            score += 30
            reasons.append(f"外資連買{fb}日")
        elif fb >= 3:
            score += 22
            reasons.append(f"外資連買{fb}日")
        elif fb >= 1:
            score += 10
        elif fb <= -3:
            score -= 15
            reasons.append(f"外資連賣{abs(fb)}日（警示）")

        # 量比
        vr = float(data.get("volume_ratio", 1.0))
        if vr > 2.0:
            score += 30
            reasons.append(f"爆量{vr:.1f}x（突破確認）")
        elif vr > 1.5:
            score += 22
            reasons.append(f"放量{vr:.1f}x")
        elif vr > 1.2:
            score += 14
            reasons.append(f"量比{vr:.1f}x")
        elif vr < 0.7:
            score -= 5  # 縮量弱

        return max(0.0, min(100.0, score)), reasons

    # ── 策略 B：價值 ──────────────────────────────────────────────────────

    def value_strategy(self, data: dict) -> tuple[float, list[str]]:
        """
        價值存股評分：
          dividend_yield  40 分
          pe_ratio        35 分
          eps_stability   25 分
        """
        score   = 0.0
        reasons = []

        # 殖利率
        dy = float(data.get("dividend_yield", 0.0))
        if dy >= 8:
            score += 40
            reasons.append(f"殖利率{dy:.1f}%（超高息）")
        elif dy >= 6:
            score += 33
            reasons.append(f"殖利率{dy:.1f}%（高息）")
        elif dy >= 5:
            score += 25
            reasons.append(f"殖利率{dy:.1f}%")
        elif dy >= 3:
            score += 12
        else:
            score -= 5  # 低息

        # 本益比
        pe = float(data.get("pe_ratio", 30.0))
        if 0 < pe < 8:
            score += 35
            reasons.append(f"PE={pe:.1f}（深度低估）")
        elif 8 <= pe < 12:
            score += 28
            reasons.append(f"PE={pe:.1f}（低估）")
        elif 12 <= pe < 15:
            score += 20
            reasons.append(f"PE={pe:.1f}（合理）")
        elif 15 <= pe < 20:
            score += 10
        elif pe > 30:
            score -= 10  # 高估

        # EPS 穩定度（0=不穩，1=完全穩定）
        eps_s = float(data.get("eps_stability", 0.5))
        if eps_s > 0.85:
            score += 25
            reasons.append("EPS 連年穩定成長")
        elif eps_s > 0.70:
            score += 17
            reasons.append("EPS 穩定")
        elif eps_s > 0.55:
            score += 10
        elif eps_s < 0.40:
            score -= 8
            reasons.append("EPS 波動大（謹慎）")

        return max(0.0, min(100.0, score)), reasons

    # ── 策略 C：籌碼 ──────────────────────────────────────────────────────

    def chip_strategy(self, data: dict) -> tuple[float, list[str]]:
        """
        籌碼追蹤評分：
          外資     40 分（weight 0.40）
          投信     30 分（weight 0.30）
          自營商   15 分（weight 0.15）
          集中度   15 分
        """
        score   = 0.0
        reasons = []

        # 外資
        fn = float(data.get("foreign_net", 0))
        if fn > 5000:
            f_score = 40
            reasons.append(f"外資大買{fn:.0f}張")
        elif fn > 2000:
            f_score = 32
            reasons.append(f"外資買超{fn:.0f}張")
        elif fn > 500:
            f_score = 22
            reasons.append(f"外資淨買{fn:.0f}張")
        elif fn > 0:
            f_score = 10
        elif fn < -1000:
            f_score = -10
            reasons.append(f"外資賣超{abs(fn):.0f}張（警示）")
        else:
            f_score = 0
        score += f_score * 0.40

        # 投信
        tn = float(data.get("trust_net", 0))
        if tn > 500:
            t_score = 30
            reasons.append(f"投信連買{tn:.0f}張")
        elif tn > 200:
            t_score = 22
            reasons.append(f"投信淨買{tn:.0f}張")
        elif tn > 50:
            t_score = 14
        elif tn < -200:
            t_score = -10
        else:
            t_score = 0
        score += t_score * 0.30

        # 自營商
        dn = float(data.get("dealer_net", 0))
        if dn > 200:
            d_score = 15
            reasons.append(f"自營商淨買{dn:.0f}張")
        elif dn > 50:
            d_score = 10
        elif dn < -100:
            d_score = -5
        else:
            d_score = 0
        score += d_score * 0.15

        # 籌碼集中度
        cc = float(data.get("chip_concentration", 50.0))
        if cc > 80:
            score += 15
            reasons.append(f"籌碼高度集中{cc:.0f}%")
        elif cc > 65:
            score += 10
            reasons.append(f"籌碼集中{cc:.0f}%")
        elif cc > 50:
            score += 5

        return max(0.0, min(100.0, score)), reasons

    # ── 信心指數計算 ──────────────────────────────────────────────────────

    def calc_confidence(
        self,
        composite_score: float,
        data: dict,
    ) -> float:
        """
        confidence = 回測夏普分 × 0.30 + 模型預測分 × 0.40 + 訊號強度分 × 0.30

        若無回測/模型資料，以 composite_score 補足。
        """
        # ── 訊號強度（= composite_score 直接轉換）
        signal_score = composite_score

        # ── 回測夏普值 → 0~100 分
        backtest_sharpe = data.get("backtest_sharpe")
        if backtest_sharpe is not None:
            bt_score = min(100, max(0, (float(backtest_sharpe) + 1) / 3 * 100))
        else:
            bt_score = composite_score  # fallback

        # ── 模型預測報酬 → 0~100 分
        pred_ret = data.get("pred_ret")
        if pred_ret is not None:
            # +5% → 100分，-5% → 0分，線性
            pred_score = min(100, max(0, (float(pred_ret) + 0.05) / 0.10 * 100))
        else:
            pred_score = composite_score  # fallback

        confidence = (
            bt_score    * 0.30 +
            pred_score  * 0.40 +
            signal_score * 0.30
        )
        return round(min(100, max(0, confidence)), 1)

    # ── 風險評估 ──────────────────────────────────────────────────────────

    def calc_risk(self, data: dict) -> tuple[float, RiskLevel]:
        """
        risk_score = volatility × 0.5 + max_drawdown × 0.5
        < 0.20 → 低　　0.20~0.40 → 中　　> 0.40 → 高
        """
        vol = float(data.get("volatility", 0.015))
        dd  = float(data.get("max_drawdown", 0.10))
        # 年化波動 = 日波動 × sqrt(252)
        annual_vol = vol * (252 ** 0.5)
        risk_score = annual_vol * 0.5 + dd * 0.5

        if risk_score < 0.20:
            level = RiskLevel.LOW
        elif risk_score < 0.40:
            level = RiskLevel.MEDIUM
        else:
            level = RiskLevel.HIGH

        return round(risk_score, 3), level

    # ── 目標價 / 停損 ─────────────────────────────────────────────────────

    def calc_targets(self, data: dict, risk_level: RiskLevel) -> tuple[float, float, int]:
        """
        target_price = close × (1 + target_pct)
        stop_loss    = close - ATR14 × atr_mult
        holding_days 根據風險等級
        """
        close = float(data.get("close", 100))
        atr   = float(data.get("atr14", close * 0.015))

        if risk_level == RiskLevel.LOW:
            target_pct = 0.10;  atr_mult = 1.5;  holding = 20
        elif risk_level == RiskLevel.MEDIUM:
            target_pct = 0.15;  atr_mult = 2.0;  holding = 10
        else:
            target_pct = 0.20;  atr_mult = 2.5;  holding = 5

        target    = round(close * (1 + target_pct), 1)
        stop_loss = round(close - atr * atr_mult, 1)
        return target, stop_loss, holding

    # ── 主評估函式 ────────────────────────────────────────────────────────

    def evaluate(
        self,
        data:     dict,
        strategy: str = "composite",
        regime:   str = "unknown",
    ) -> StrategySignal:
        """
        評估單股並回傳 StrategySignal。

        strategy: "composite" / "momentum" / "value" / "chip"
        regime:   "bull" / "bear" / "sideways" / "volatile" / "unknown"
        """
        stock_id = str(data.get("stock_id", "????"))
        name     = str(data.get("name", stock_id))

        # ── 各策略評分
        mom_score,  mom_reasons  = self.momentum_strategy(data)
        val_score,  val_reasons  = self.value_strategy(data)
        chip_score, chip_reasons = self.chip_strategy(data)

        # ── 複合評分（依盤態加權）
        w = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["unknown"])
        if strategy == "momentum":
            composite = mom_score
            all_reasons = mom_reasons
        elif strategy == "value":
            composite = val_score
            all_reasons = val_reasons
        elif strategy == "chip":
            composite = chip_score
            all_reasons = chip_reasons
        else:
            composite = (
                mom_score  * w["momentum"] +
                val_score  * w["value"]    +
                chip_score * w["chip"]
            )
            # 合併理由（去重，最多 5 條）
            seen, all_reasons = set(), []
            for r in mom_reasons + chip_reasons + val_reasons:
                if r not in seen:
                    seen.add(r)
                    all_reasons.append(r)
            all_reasons = all_reasons[:5]

        # ── 信心指數
        confidence = self.calc_confidence(composite, data)

        # ── 行動建議
        action = Action.SELL
        for threshold, act in ACTION_THRESHOLDS:
            if confidence >= threshold:
                action = act
                break

        # ── 風險評估
        risk_score, risk_level = self.calc_risk(data)

        # ── 目標價 / 停損
        target, stop_loss, holding_days = self.calc_targets(data, risk_level)

        # ── 技術面補充理由
        close = float(data.get("close", 0))
        ma20  = float(data.get("ma20", close))
        ma60  = float(data.get("ma60", close))
        if close > ma20 > ma60:
            if "均線多頭排列" not in all_reasons:
                all_reasons.insert(0, "均線多頭排列")
        if float(data.get("macd_golden", 0)):
            if "MACD金叉" not in all_reasons:
                all_reasons.append("MACD金叉")

        return StrategySignal(
            stock_id=stock_id,
            name=name,
            action=action,
            confidence=confidence,
            reasons=all_reasons[:5],
            target_price=target,
            stop_loss=stop_loss,
            holding_days=holding_days,
            risk_level=risk_level,
            strategy=strategy,
            momentum_score=round(mom_score, 1),
            value_score=round(val_score, 1),
            chip_score=round(chip_score, 1),
            composite_score=round(composite, 1),
            risk_score=risk_score,
        )

    def batch_evaluate(
        self,
        stocks:   list[dict],
        strategy: str = "composite",
        regime:   str = "unknown",
        min_confidence: float = 45.0,
    ) -> list[StrategySignal]:
        """
        批次評估多檔股票，按信心指數排序，過濾低信心。
        stocks: [data_dict, ...]
        """
        signals = [self.evaluate(s, strategy=strategy, regime=regime) for s in stocks]
        signals = [s for s in signals if s.confidence >= min_confidence]
        return sorted(signals, key=lambda s: -s.confidence)

    def compare(self, data_a: dict, data_b: dict, regime: str = "unknown") -> dict:
        """
        比較兩檔股票，回傳分析報告。
        """
        sig_a = self.evaluate(data_a, regime=regime)
        sig_b = self.evaluate(data_b, regime=regime)

        winner_conf  = sig_a.stock_id if sig_a.confidence >= sig_b.confidence else sig_b.stock_id
        winner_risk  = sig_a.stock_id if sig_a.risk_score  <= sig_b.risk_score  else sig_b.stock_id

        if sig_a.confidence > sig_b.confidence + 10:
            recommend = sig_a.stock_id
            rec_reason = f"信心明顯較高（{sig_a.confidence:.0f} vs {sig_b.confidence:.0f}）"
        elif sig_b.confidence > sig_a.confidence + 10:
            recommend = sig_b.stock_id
            rec_reason = f"信心明顯較高（{sig_b.confidence:.0f} vs {sig_a.confidence:.0f}）"
        elif sig_a.risk_score < sig_b.risk_score:
            recommend = sig_a.stock_id
            rec_reason = f"風險較低（{sig_a.risk_level.value} vs {sig_b.risk_level.value}）"
        else:
            recommend = sig_b.stock_id
            rec_reason = f"風險較低（{sig_b.risk_level.value} vs {sig_a.risk_level.value}）"

        return {
            sig_a.stock_id: sig_a.to_dict(),
            sig_b.stock_id: sig_b.to_dict(),
            "compare": {
                "higher_confidence": winner_conf,
                "lower_risk":        winner_risk,
                "recommend":         recommend,
                "reason":            rec_reason,
            },
        }


# ── Mock 資料 ────────────────────────────────────────────────────────────────

MOCK_STOCKS: list[dict] = [
    {
        "stock_id": "2330", "name": "台積電",
        "momentum_20d": 1.08, "foreign_buy_days": 5, "volume_ratio": 1.6,
        "dividend_yield": 2.5, "pe_ratio": 22.0, "eps_stability": 0.90,
        "foreign_net": 8000, "trust_net": 600, "dealer_net": 200, "chip_concentration": 78,
        "volatility": 0.018, "max_drawdown": 0.15,
        "close": 850.0, "ma20": 820.0, "ma60": 790.0, "atr14": 18.0,
        "backtest_sharpe": 1.2, "pred_ret": 0.04,
    },
    {
        "stock_id": "0056", "name": "元大高股息",
        "momentum_20d": 1.01, "foreign_buy_days": 1, "volume_ratio": 1.1,
        "dividend_yield": 7.5, "pe_ratio": 13.0, "eps_stability": 0.85,
        "foreign_net": 100, "trust_net": 50, "dealer_net": 20, "chip_concentration": 60,
        "volatility": 0.008, "max_drawdown": 0.05,
        "close": 36.5, "ma20": 35.8, "ma60": 34.5, "atr14": 0.5,
        "backtest_sharpe": 0.8, "pred_ret": 0.01,
    },
    {
        "stock_id": "2454", "name": "聯發科",
        "momentum_20d": 1.12, "foreign_buy_days": 7, "volume_ratio": 2.1,
        "dividend_yield": 3.5, "pe_ratio": 18.0, "eps_stability": 0.75,
        "foreign_net": 3000, "trust_net": 800, "dealer_net": 150, "chip_concentration": 72,
        "volatility": 0.022, "max_drawdown": 0.22,
        "close": 1150.0, "ma20": 1080.0, "ma60": 1020.0, "atr14": 28.0,
        "backtest_sharpe": 1.5, "pred_ret": 0.06,
    },
    {
        "stock_id": "2412", "name": "中華電",
        "momentum_20d": 0.99, "foreign_buy_days": -2, "volume_ratio": 0.8,
        "dividend_yield": 6.2, "pe_ratio": 22.0, "eps_stability": 0.92,
        "foreign_net": -200, "trust_net": 30, "dealer_net": -50, "chip_concentration": 55,
        "volatility": 0.007, "max_drawdown": 0.04,
        "close": 118.0, "ma20": 119.5, "ma60": 120.0, "atr14": 1.0,
        "backtest_sharpe": 0.5, "pred_ret": 0.005,
    },
    {
        "stock_id": "2317", "name": "鴻海",
        "momentum_20d": 1.06, "foreign_buy_days": 4, "volume_ratio": 1.4,
        "dividend_yield": 4.8, "pe_ratio": 11.0, "eps_stability": 0.70,
        "foreign_net": 2500, "trust_net": 400, "dealer_net": 100, "chip_concentration": 65,
        "volatility": 0.016, "max_drawdown": 0.18,
        "close": 118.5, "ma20": 115.0, "ma60": 112.0, "atr14": 2.5,
        "backtest_sharpe": 0.9, "pred_ret": 0.03,
    },
]


# ── 獨立測試 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    engine = StrategyEngine()

    print("=== 複合策略評估（多頭盤態）===")
    for stock in MOCK_STOCKS:
        sig = engine.evaluate(stock, strategy="composite", regime="bull")
        print(f"  {sig.stock_id} {sig.name:8s} "
              f"信心={sig.confidence:5.1f}  {sig.action.value:5s}  "
              f"風險={sig.risk_level.value}  "
              f"目標={sig.target_price:7.1f}  停損={sig.stop_loss:7.1f}")

    print("\n=== 批次篩選（信心>=60）===")
    top = engine.batch_evaluate(MOCK_STOCKS, regime="bull", min_confidence=60)
    for s in top:
        print(f"  {s.stock_id} {s.name} {s.confidence:.0f} {s.action.value}")
        print(f"    理由: {', '.join(s.reasons)}")

    print("\n=== 比較：2330 vs 2454 ===")
    result = engine.compare(MOCK_STOCKS[0], MOCK_STOCKS[2], regime="bull")
    cmp = result["compare"]
    print(f"  信心較高: {cmp['higher_confidence']}")
    print(f"  風險較低: {cmp['lower_risk']}")
    print(f"  建議選擇: {cmp['recommend']}（{cmp['reason']}）")

    print("\n=== LINE 格式輸出 ===")
    sig = engine.evaluate(MOCK_STOCKS[0], regime="bull")
    print(sig.to_line_text())
