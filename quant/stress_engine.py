"""
stress_engine.py — 市場壓力測試引擎

量化市場壓力程度，早期偵測系統性風險：
  - 流動性壓力（spread 擴大、成交量驟降）
  - 相關性驟升（避險不分青紅皂白砍倉）
  - 波動率 spike（VIX-like 指標）
  - 外資撤資速度
  - 融資斷頭風險
  - 期現價差異常

Stress Level 0-100：
  0-20:  正常
  20-40: 輕度壓力
  40-60: 中度壓力（提高警覺）
  60-80: 高度壓力（減倉）
  80-100: 系統性危機（清倉）
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

STRESS_LEVEL_ZH = {
    (0,  20): ("CALM",     "正常運行", "🟢"),
    (20, 40): ("MILD",     "輕度壓力", "🟡"),
    (40, 60): ("MODERATE", "中度警戒", "🟠"),
    (60, 80): ("HIGH",     "高度壓力", "🔴"),
    (80, 101):("CRISIS",   "系統危機", "💥"),
}


def _stress_zone(score: float) -> tuple[str, str, str]:
    for (lo, hi), val in STRESS_LEVEL_ZH.items():
        if lo <= score < hi:
            return val
    return "CALM", "正常", "🟢"


@dataclass
class StressIndicator:
    name:    str
    raw:     float
    score:   float   # 0-100，越高=壓力越大
    weight:  float
    detail:  str = ""

    @property
    def weighted(self) -> float:
        return self.score * self.weight


@dataclass
class TailRisk:
    scenario:    str
    probability: float   # 0-1
    impact:      str     # "LOW" / "MED" / "HIGH"
    hedge:       str     # 建議對沖方式

    def to_text(self) -> str:
        icons = {"LOW": "🟡", "MED": "🟠", "HIGH": "🔴"}
        return f"{icons.get(self.impact,'⚪')} {self.scenario} ({self.probability:.0%}) → {self.hedge}"


@dataclass
class StressResult:
    stress_score:  float
    level_key:     str
    level_zh:      str
    level_emoji:   str
    indicators:    list[StressIndicator] = field(default_factory=list)
    tail_risks:    list[TailRisk]        = field(default_factory=list)
    action_hints:  list[str]             = field(default_factory=list)
    ts:            str = field(default_factory=lambda: datetime.now().isoformat())

    def to_line_text(self) -> str:
        bar_len = int(self.stress_score / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines = [
            f"{self.level_emoji} 市場壓力儀表",
            f"[{bar}]",
            f"壓力指數：{self.stress_score:.1f}/100  {self.level_zh}",
        ]
        if self.action_hints:
            lines.append("")
            lines.append("📋 建議行動：")
            for hint in self.action_hints:
                lines.append(f"  • {hint}")
        if self.tail_risks:
            lines.append("")
            lines.append("⚡ 尾部風險：")
            for tr in self.tail_risks[:3]:
                lines.append(f"  {tr.to_text()}")
        return "\n".join(lines)


def _norm(v: float, lo: float, hi: float) -> float:
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100))


async def compute_stress(market_data: dict | None = None) -> StressResult:
    """
    market_data:
    {
      bid_ask_spread_pct: float,   # 買賣價差擴大比例（vs 平均）
      volume_drop_pct: float,      # 成交量vs20日均量的下降比例
      correlation_spike: float,    # 個股相關係數急升 0-1
      volatility_ratio: float,     # 波動率 vs 30日均值
      foreign_sell_rate: float,    # 外資賣超速度（億/日）
      margin_call_risk: float,     # 融資維持率接近警戒比例 0-1
      futures_premium: float,      # 期現價差異常（點數）
      credit_spread: float,        # 信用利差（bps）
    }
    """
    if not market_data:
        market_data = {
            "bid_ask_spread_pct": 0.15,
            "volume_drop_pct":    0.10,
            "correlation_spike":  0.30,
            "volatility_ratio":   1.20,
            "foreign_sell_rate":  5.0,
            "margin_call_risk":   0.08,
            "futures_premium":    30.0,
            "credit_spread":      80.0,
        }

    indicators: list[StressIndicator] = [
        StressIndicator("買賣價差擴大", market_data.get("bid_ask_spread_pct", 0),
                        _norm(market_data.get("bid_ask_spread_pct", 0), 0, 1.0), 1.2,
                        "流動性惡化"),
        StressIndicator("成交量萎縮",  market_data.get("volume_drop_pct", 0),
                        _norm(market_data.get("volume_drop_pct", 0), 0, 0.8), 1.0,
                        "市場觀望"),
        StressIndicator("相關性急升",  market_data.get("correlation_spike", 0),
                        market_data.get("correlation_spike", 0) * 100, 1.5,
                        "避險砍倉"),
        StressIndicator("波動率倍數",  market_data.get("volatility_ratio", 1),
                        _norm(market_data.get("volatility_ratio", 1), 1.0, 5.0), 1.3,
                        "恐慌加劇"),
        StressIndicator("外資撤資速度", market_data.get("foreign_sell_rate", 0),
                        _norm(market_data.get("foreign_sell_rate", 0), 0, 50), 1.2,
                        "資本外流"),
        StressIndicator("融資斷頭風險", market_data.get("margin_call_risk", 0),
                        market_data.get("margin_call_risk", 0) * 100, 1.4,
                        "強制出場壓力"),
        StressIndicator("期現價差異常", market_data.get("futures_premium", 0),
                        _norm(abs(market_data.get("futures_premium", 0)), 0, 200), 0.8,
                        "套利中斷"),
        StressIndicator("信用利差",    market_data.get("credit_spread", 0),
                        _norm(market_data.get("credit_spread", 0), 0, 300), 1.0,
                        "風險溢酬"),
    ]

    total_w = sum(i.weight for i in indicators)
    stress_score = sum(i.weighted for i in indicators) / total_w if total_w else 50.0

    lk, lz, le = _stress_zone(stress_score)

    # 行動建議
    action_hints: list[str] = []
    if stress_score >= 80:
        action_hints = ["立即清倉高風險部位", "持有現金等待反轉", "考慮反向避險"]
    elif stress_score >= 60:
        action_hints = ["減倉至50%以下", "嚴守停損", "暫停新建倉"]
    elif stress_score >= 40:
        action_hints = ["控制倉位，避免槓桿", "偏好防禦性個股"]
    else:
        action_hints = ["正常操作", "注意個股風控"]

    # 尾部風險
    tail_risks: list[TailRisk] = []
    if market_data.get("foreign_sell_rate", 0) > 20:
        tail_risks.append(TailRisk("外資大逃殺", 0.35, "HIGH", "多空對沖期貨"))
    if market_data.get("correlation_spike", 0) > 0.7:
        tail_risks.append(TailRisk("系統性拋售", 0.25, "HIGH", "現金部位提高"))
    if market_data.get("margin_call_risk", 0) > 0.3:
        tail_risks.append(TailRisk("融資斷頭踩踏", 0.20, "MED", "避開高融資股"))
    if market_data.get("volatility_ratio", 1) > 3:
        tail_risks.append(TailRisk("波動率炸彈", 0.15, "MED", "縮小部位大小"))

    return StressResult(
        stress_score = round(stress_score, 1),
        level_key    = lk,
        level_zh     = lz,
        level_emoji  = le,
        indicators   = indicators,
        tail_risks   = tail_risks,
        action_hints = action_hints,
    )
