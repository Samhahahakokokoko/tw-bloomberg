"""
decision_explainability.py — 決策可解釋性引擎

每個決策都要說明原因，格式：

🤖 2330 台積電 決策說明
最終決策：買進  信心指數：82/100

正面因子：
✅ 動能因子：+18分（20日動能強）
✅ 籌碼因子：+22分（外資連買5日）
...

負面因子：
⚠️ 過熱懲罰：-8分（Euphoria 72）
...

資料品質：
✅ 所有資料為 Live 數據
✅ 平均可信度：93%
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FactorContribution:
    name:        str
    score_delta: float     # 正=正面貢獻, 負=負面
    description: str
    positive:    bool = True

    @property
    def icon(self) -> str:
        return "✅" if self.positive and self.score_delta >= 0 else "⚠️"

    def format(self) -> str:
        sign = "+" if self.score_delta >= 0 else ""
        return f"{self.icon} {self.name}：{sign}{self.score_delta:.0f}分（{self.description}）"


@dataclass
class DataQualitySummary:
    avg_confidence:  float
    mock_count:      int
    stale_count:     int
    total_records:   int
    sources:         list[str] = field(default_factory=list)

    @property
    def is_all_live(self) -> bool:
        return self.mock_count == 0

    @property
    def confidence_pct(self) -> int:
        return int(self.avg_confidence * 100)

    def format(self) -> str:
        lines = []
        if self.is_all_live:
            lines.append("✅ 所有資料為 Live 數據")
        else:
            lines.append(f"⚠️ 含 {self.mock_count} 筆示範資料")
        lines.append(f"{'✅' if self.avg_confidence >= 0.85 else '⚠️'} 平均可信度：{self.confidence_pct}%")
        if self.stale_count > 0:
            lines.append(f"⚠️ {self.stale_count} 筆資料過時")
        if self.sources:
            lines.append(f"📡 來源：{'、'.join(set(self.sources))}")
        return "\n".join(lines)


@dataclass
class DecisionExplanation:
    stock_id:       str
    stock_name:     str
    action:         str           # buy / sell / watch / skipped
    final_confidence: float
    base_score:     float
    factors:        list[FactorContribution] = field(default_factory=list)
    data_quality:   Optional[DataQualitySummary] = None
    skip_reason:    str = ""
    ts:             str = field(default_factory=lambda: datetime.now().isoformat())

    _ACTION_ZH = {
        "buy":     "買進",
        "add":     "加碼",
        "reduce":  "減碼",
        "sell":    "賣出",
        "watch":   "觀察",
        "skipped": "跳過（資料不足）",
        "blocked": "停止（Kill Switch）",
    }

    def format_line(self) -> str:
        action_zh = self._ACTION_ZH.get(self.action, self.action)
        bar = "█" * int(self.final_confidence / 10) + "░" * (10 - int(self.final_confidence / 10))

        lines = [
            f"🤖 {self.stock_id} {self.stock_name} 決策說明",
            "",
            f"最終決策：{action_zh}",
            f"信心指數：[{bar}] {self.final_confidence:.0f}/100",
        ]

        if self.skip_reason:
            lines.append(f"\n⛔ 跳過原因：{self.skip_reason}")
            return "\n".join(lines)

        positive = [f for f in self.factors if f.score_delta >= 0]
        negative = [f for f in self.factors if f.score_delta < 0]

        if positive:
            lines.append("")
            lines.append("正面因子：")
            for f in sorted(positive, key=lambda x: -x.score_delta):
                lines.append(f"  {f.format()}")

        if negative:
            lines.append("")
            lines.append("負面因子：")
            for f in sorted(negative, key=lambda x: x.score_delta):
                lines.append(f"  {f.format()}")

        if self.data_quality:
            lines.append("")
            lines.append("資料品質：")
            for l in self.data_quality.format().split("\n"):
                lines.append(f"  {l}")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "stock_id":          self.stock_id,
            "stock_name":        self.stock_name,
            "action":            self.action,
            "final_confidence":  round(self.final_confidence, 1),
            "factors": [
                {"name": f.name, "delta": f.score_delta, "desc": f.description}
                for f in self.factors
            ],
            "data_quality": {
                "avg_confidence": self.data_quality.avg_confidence,
                "mock_count":     self.data_quality.mock_count,
                "stale_count":    self.data_quality.stale_count,
            } if self.data_quality else None,
            "skip_reason": self.skip_reason,
        }


def build_explanation(
    stock_id:    str,
    stock_name:  str,
    action:      str,
    confidence:  float,
    reasons:     list[str],
    movers_score: float = 0.0,
    scanner_layer: str  = "",
    filter_passed: bool = True,
    research_ok:   bool = True,
    consensus_boost: float = 0.0,
    euphoria_penalty: float = 0.0,
    data_quality: Optional[DataQualitySummary] = None,
    skip_reason: str = "",
) -> DecisionExplanation:
    """
    從 decision_engine 的各層輸出組裝可解釋性說明。
    """
    factors: list[FactorContribution] = []

    # 動能分
    if movers_score > 0:
        factors.append(FactorContribution(
            "動能因子",
            round(movers_score * 0.3, 1),
            f"動能分數 {movers_score:.0f}",
        ))

    # 分類層
    if scanner_layer:
        layer_score = {"core": 20.0, "medium": 12.0, "satellite": 6.0}.get(scanner_layer, 0)
        if layer_score:
            factors.append(FactorContribution(
                "分類位階",
                layer_score,
                f"Scanner 分類：{scanner_layer}",
            ))

    # Research 通過
    if research_ok:
        factors.append(FactorContribution("基本面審查", 15.0, "Research Checklist 通過"))
    else:
        factors.append(FactorContribution("基本面審查", -20.0, "Research Checklist 未通過", positive=False))

    # 分析師共識
    if consensus_boost != 0:
        factors.append(FactorContribution(
            "分析師共識",
            consensus_boost,
            "S/A 級分析師共識" if consensus_boost > 0 else "分析師高分歧",
            positive=consensus_boost > 0,
        ))

    # 過熱懲罰
    if euphoria_penalty != 0:
        factors.append(FactorContribution(
            "過熱懲罰",
            -abs(euphoria_penalty),
            f"Euphoria 過熱扣分",
            positive=False,
        ))

    # 其他 reasons 轉 factor
    for r in reasons:
        if "外資" in r:
            factors.append(FactorContribution("籌碼因子", 10.0, r))
        elif "營收" in r or "EPS" in r:
            factors.append(FactorContribution("基本面", 8.0, r))

    return DecisionExplanation(
        stock_id          = stock_id,
        stock_name        = stock_name,
        action            = action,
        final_confidence  = confidence,
        base_score        = confidence,
        factors           = factors,
        data_quality      = data_quality,
        skip_reason       = skip_reason,
    )
