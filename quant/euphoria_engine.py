"""
euphoria_engine.py — 市場過熱/恐慌溫度計

綜合 8 個過熱指標：
  1. 漲停家數 / 總家數比例
  2. RSI>80 比例
  3. 融資餘額周增率
  4. 新高比例（52週）
  5. 媒體/社群正面情緒指數
  6. 大盤量能比（vs 20日均量）
  7. 本益比溢價（vs 歷史均值）
  8. 散戶開戶數月增率

Euphoria Score 0-100：
  0-25:  極度悲觀（FEAR ZONE）
  25-45: 偏空觀望
  45-55: 中性區間
  55-75: 樂觀偏多
  75-90: 過熱警戒
  90-100: 極度狂熱（SELL SIGNAL）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

ZONE_LABEL = {
    (0,  25):  ("FEAR",     "極度恐慌",  "🔵"),
    (25, 45):  ("CAUTION",  "偏空觀望",  "🟡"),
    (45, 55):  ("NEUTRAL",  "中性區間",  "⚪"),
    (55, 75):  ("BULLISH",  "樂觀偏多",  "🟠"),
    (75, 90):  ("WARNING",  "過熱警戒",  "🔴"),
    (90, 101): ("EUPHORIA", "極度狂熱",  "💥"),
}


def _get_zone(score: float) -> tuple[str, str, str]:
    for (lo, hi), (key, zh, emoji) in ZONE_LABEL.items():
        if lo <= score < hi:
            return key, zh, emoji
    return "NEUTRAL", "中性", "⚪"


@dataclass
class EuphoriaIndicator:
    name:    str
    value:   float   # 原始值
    score:   float   # 0-100 正規化
    weight:  float   # 加權
    bullish: bool    # True=過熱方向

    @property
    def weighted_score(self) -> float:
        return self.score * self.weight


@dataclass
class EuphoriaResult:
    euphoria_score:   float
    zone_key:         str
    zone_zh:          str
    zone_emoji:       str
    indicators:       list[EuphoriaIndicator] = field(default_factory=list)
    top_hot_stocks:   list[str] = field(default_factory=list)
    warning_signals:  list[str] = field(default_factory=list)
    ts:               str = field(default_factory=lambda: datetime.now().isoformat())

    def to_line_text(self) -> str:
        score_int = int(self.euphoria_score)
        bar_len   = score_int // 5
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines = [
            f"{self.zone_emoji} 市場溫度計",
            f"[{bar}]",
            f"過熱分數：{self.euphoria_score:.1f}/100",
            f"區間：{self.zone_zh}（{self.zone_key}）",
        ]
        if self.warning_signals:
            lines.append("")
            lines.append("⚠️ 警示信號：")
            for w in self.warning_signals[:3]:
                lines.append(f"  • {w}")
        if self.top_hot_stocks:
            lines.append(f"過熱個股：{'、'.join(self.top_hot_stocks[:5])}")
        return "\n".join(lines)


def _normalize(value: float, lo: float, hi: float) -> float:
    """線性正規化到 0-100"""
    return max(0.0, min(100.0, (value - lo) / (hi - lo) * 100))


async def compute_euphoria(market_data: dict | None = None) -> EuphoriaResult:
    """
    market_data 格式：
    {
      limit_up_ratio: float,       # 漲停比例 0-1
      rsi80_ratio: float,          # RSI>80比例
      margin_growth_ww: float,     # 融資周增率
      new_high_ratio: float,       # 52週新高比例
      sentiment_index: float,      # 社群情緒 0-100
      volume_ratio: float,         # 量能比
      pe_premium: float,           # 本益比溢價 %
      retail_account_growth: float, # 散戶開戶月增率
      hot_stocks: list[str],
    }
    """
    if not market_data:
        market_data = {
            "limit_up_ratio":        0.04,
            "rsi80_ratio":           0.15,
            "margin_growth_ww":      0.03,
            "new_high_ratio":        0.25,
            "sentiment_index":       62.0,
            "volume_ratio":          1.4,
            "pe_premium":            8.0,
            "retail_account_growth": 0.02,
            "hot_stocks":            ["3661", "2330", "6669"],
        }

    indicators: list[EuphoriaIndicator] = [
        EuphoriaIndicator("漲停家數比",    market_data.get("limit_up_ratio", 0),
                          _normalize(market_data.get("limit_up_ratio", 0), 0, 0.15), 1.5, True),
        EuphoriaIndicator("RSI>80 比例",  market_data.get("rsi80_ratio", 0),
                          _normalize(market_data.get("rsi80_ratio", 0), 0, 0.5),    1.2, True),
        EuphoriaIndicator("融資周增率",   market_data.get("margin_growth_ww", 0),
                          _normalize(market_data.get("margin_growth_ww", 0), -0.1, 0.2), 1.3, True),
        EuphoriaIndicator("52W新高比例",  market_data.get("new_high_ratio", 0),
                          _normalize(market_data.get("new_high_ratio", 0), 0, 0.6),  1.0, True),
        EuphoriaIndicator("情緒指數",     market_data.get("sentiment_index", 50),
                          market_data.get("sentiment_index", 50),                     1.0, True),
        EuphoriaIndicator("量能比",       market_data.get("volume_ratio", 1),
                          _normalize(market_data.get("volume_ratio", 1), 0.5, 3.0),  1.1, True),
        EuphoriaIndicator("本益比溢價",   market_data.get("pe_premium", 0),
                          _normalize(market_data.get("pe_premium", 0), -20, 50),      0.8, True),
        EuphoriaIndicator("散戶開戶增率", market_data.get("retail_account_growth", 0),
                          _normalize(market_data.get("retail_account_growth", 0), -0.05, 0.15), 0.8, True),
    ]

    total_weight = sum(i.weight for i in indicators)
    weighted_sum = sum(i.weighted_score for i in indicators)
    euphoria_score = weighted_sum / total_weight if total_weight > 0 else 50.0

    zone_key, zone_zh, zone_emoji = _get_zone(euphoria_score)

    warnings: list[str] = []
    for ind in indicators:
        if ind.score >= 85:
            warnings.append(f"{ind.name} 極度偏高（{ind.value:.2%}）" if ind.value < 10 else f"{ind.name} 極度偏高（{ind.value:.1f}）")

    return EuphoriaResult(
        euphoria_score  = round(euphoria_score, 1),
        zone_key        = zone_key,
        zone_zh         = zone_zh,
        zone_emoji      = zone_emoji,
        indicators      = indicators,
        top_hot_stocks  = market_data.get("hot_stocks", []),
        warning_signals = warnings,
    )
