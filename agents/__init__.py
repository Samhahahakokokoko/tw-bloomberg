"""
agents/ — Multi-Agent 市場情報系統

每個 Agent 回傳標準化的 AgentVote：
  opinion: bullish / neutral / bearish
  confidence: 0-1
  reasons: list[str]
  veto: bool (只有 risk_agent 使用)
"""
from .committee_engine import CommitteeDecision, run_committee
