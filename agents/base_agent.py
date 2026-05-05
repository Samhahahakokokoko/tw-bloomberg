"""base_agent.py — 所有 Agent 的共用基礎"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class AgentVote:
    agent_name:  str
    opinion:     str          # bullish / neutral / bearish
    confidence:  float        # 0-1
    reasons:     list[str]    = field(default_factory=list)
    veto:        bool         = False     # 只有 risk_agent 用
    veto_reason: str          = ""
    data_quality: float       = 1.0      # 0-1，本次資料品質
    ts:          str          = field(default_factory=lambda: datetime.now().isoformat())

    OPINION_ICONS = {"bullish": "🟢", "neutral": "🟡", "bearish": "🔴"}

    @property
    def icon(self) -> str:
        if self.veto:
            return "🛡️"
        return self.OPINION_ICONS.get(self.opinion, "⚪")

    @property
    def opinion_zh(self) -> str:
        mapping = {"bullish": "看多", "neutral": "中性", "bearish": "看空"}
        return mapping.get(self.opinion, self.opinion)

    def format_row(self) -> str:
        veto_note = f"（否決：{self.veto_reason}）" if self.veto else ""
        return (f"{self.icon} {self.agent_name}：{self.opinion_zh}"
                f"（{', '.join(self.reasons[:1])}）{veto_note}")

    def to_dict(self) -> dict:
        return {
            "agent":       self.agent_name,
            "opinion":     self.opinion,
            "confidence":  round(self.confidence, 3),
            "reasons":     self.reasons,
            "veto":        self.veto,
            "veto_reason": self.veto_reason,
            "data_quality": round(self.data_quality, 3),
        }
