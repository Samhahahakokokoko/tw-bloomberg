"""
committee_engine.py — Multi-Agent 委員會投票引擎

投票規則：
  - News / YouTube / Flow / Macro 各投一票
  - Risk Agent 擁有否決權（veto=True → 無論如何不買）
  - 4票以上 bullish → 強力買進
  - 3票   bullish → 一般買進
  - 2票以下 → 觀望
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .base_agent import AgentVote

logger = logging.getLogger(__name__)


@dataclass
class CommitteeDecision:
    stock_id:     str
    stock_name:   str
    votes:        list[AgentVote]
    risk_vote:    AgentVote
    bullish_count: int
    neutral_count: int
    bearish_count: int
    final_action: str       # strong_buy / buy / watch / sell / vetoed
    confidence:   float     # 0-100
    veto_active:  bool = False
    veto_reason:  str  = ""
    ts:           str  = field(default_factory=lambda: datetime.now().isoformat())

    _ACTION_ZH = {
        "strong_buy": "強力買進",
        "buy":        "買進",
        "watch":      "觀望",
        "sell":       "賣出",
        "vetoed":     "否決（風控停止）",
    }

    def format_line(self) -> str:
        action_zh = self._ACTION_ZH.get(self.final_action, self.final_action)
        bar = "█" * int(self.confidence / 10) + "░" * (10 - int(self.confidence / 10))

        lines = [
            f"🏛️ AI委員會今日決議",
            f"",
            f"標的：{self.stock_id} {self.stock_name}",
            f"",
            f"投票結果：",
        ]
        for v in self.votes:
            lines.append(f"  {v.format_row()}")
        lines.append(f"  {self.risk_vote.format_row()}")

        lines += [
            f"",
            f"最終決議：{action_zh}（{self.bullish_count}:{self.neutral_count}:{self.bearish_count}）",
            f"信心指數：[{bar}] {self.confidence:.0f}/100",
        ]
        if self.veto_active:
            lines.append(f"\n⛔ 否決原因：{self.veto_reason}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "stock_id":     self.stock_id,
            "stock_name":   self.stock_name,
            "votes":        [v.to_dict() for v in self.votes],
            "risk_vote":    self.risk_vote.to_dict(),
            "bullish":      self.bullish_count,
            "neutral":      self.neutral_count,
            "bearish":      self.bearish_count,
            "final_action": self.final_action,
            "confidence":   round(self.confidence, 1),
            "veto":         self.veto_active,
            "veto_reason":  self.veto_reason,
            "ts":           self.ts,
        }


async def run_committee(
    stock_id:   str,
    stock_name: str = "",
    sector:     str = "",
) -> CommitteeDecision:
    """執行委員會投票"""
    import asyncio

    # 並行執行所有投票 Agent
    from agents import news_agent, youtube_agent, flow_agent, macro_agent, risk_agent

    news_v, yt_v, flow_v, macro_v, risk_v = await asyncio.gather(
        news_agent.run(stock_id, sector),
        youtube_agent.run(stock_id, sector),
        flow_agent.run(stock_id, sector),
        macro_agent.run(stock_id, sector),
        risk_agent.run(stock_id, sector),
        return_exceptions=True,
    )

    def _safe_vote(result, name: str) -> AgentVote:
        if isinstance(result, AgentVote):
            return result
        logger.warning("[Committee] %s failed: %s", name, result)
        return AgentVote(name, "neutral", 0.3, ["執行失敗"], data_quality=0.2)

    votes = [
        _safe_vote(news_v,  "新聞Agent"),
        _safe_vote(yt_v,    "YouTube Agent"),
        _safe_vote(flow_v,  "資金Agent"),
        _safe_vote(macro_v, "總經Agent"),
    ]
    risk_vote = _safe_vote(risk_v, "風控Agent")

    # 否決權
    if risk_vote.veto:
        return CommitteeDecision(
            stock_id      = stock_id,
            stock_name    = stock_name,
            votes         = votes,
            risk_vote     = risk_vote,
            bullish_count = 0,
            neutral_count = 0,
            bearish_count = len(votes),
            final_action  = "vetoed",
            confidence    = 0.0,
            veto_active   = True,
            veto_reason   = risk_vote.veto_reason,
        )

    # 統計投票
    bull = sum(1 for v in votes if v.opinion == "bullish")
    bear = sum(1 for v in votes if v.opinion == "bearish")
    neu  = len(votes) - bull - bear

    # 加權信心（考量各 Agent 的 confidence）
    weighted_conf = sum(
        v.confidence * (2 if v.opinion == "bullish" else
                        (-1 if v.opinion == "bearish" else 0))
        for v in votes
    ) / max(sum(v.confidence for v in votes), 0.01)

    base_conf = (bull / len(votes)) * 100

    if bull >= 4:
        action = "strong_buy"
        confidence = min(95, base_conf * 1.2)
    elif bull >= 3:
        action = "buy"
        confidence = base_conf
    elif bear >= 3:
        action = "sell"
        confidence = (bear / len(votes)) * 100
    else:
        action = "watch"
        confidence = 40.0

    # 儲存到 DB
    await _save_committee_result(CommitteeDecision(
        stock_id      = stock_id,
        stock_name    = stock_name,
        votes         = votes,
        risk_vote     = risk_vote,
        bullish_count = bull,
        neutral_count = neu,
        bearish_count = bear,
        final_action  = action,
        confidence    = round(confidence, 1),
    ))

    return CommitteeDecision(
        stock_id      = stock_id,
        stock_name    = stock_name,
        votes         = votes,
        risk_vote     = risk_vote,
        bullish_count = bull,
        neutral_count = neu,
        bearish_count = bear,
        final_action  = action,
        confidence    = round(confidence, 1),
    )


async def run_batch_committee(stocks: list[dict]) -> list[CommitteeDecision]:
    """批次對多檔股票執行委員會"""
    results = []
    for s in stocks:
        try:
            r = await run_committee(
                stock_id   = s.get("stock_id", ""),
                stock_name = s.get("name", ""),
                sector     = s.get("sector", ""),
            )
            results.append(r)
        except Exception as e:
            logger.warning("[Committee] batch failed for %s: %s",
                           s.get("stock_id"), e)
    return results


async def _save_committee_result(decision: CommitteeDecision):
    try:
        import json
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import CommitteeDecisionLog
        async with AsyncSessionLocal() as db:
            db.add(CommitteeDecisionLog(
                stock_id      = decision.stock_id,
                stock_name    = decision.stock_name,
                bullish_count = decision.bullish_count,
                neutral_count = decision.neutral_count,
                bearish_count = decision.bearish_count,
                final_action  = decision.final_action,
                confidence    = decision.confidence,
                veto_active   = decision.veto_active,
                veto_reason   = decision.veto_reason,
                votes_json    = json.dumps([v.to_dict() for v in decision.votes],
                                           ensure_ascii=False),
            ))
            await db.commit()
    except Exception:
        pass
