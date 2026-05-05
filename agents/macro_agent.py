"""macro_agent.py — AI 總經分析員（每週更新）"""
from __future__ import annotations
import logging
import os
from datetime import datetime
from .base_agent import AgentVote

logger = logging.getLogger(__name__)
AGENT_NAME = "總經Agent"

_MACRO_PROMPT = """你是台股總經分析師。請用繁體中文，根據當前市場環境分析總體經濟對台股的影響。

請評估：
1. 美國利率/Fed 動向對台股科技股的影響
2. 美元指數對台幣匯率的影響（台幣升值對出口股的匯損風險）
3. 費城半導體指數 vs 台股半導體的關聯與落後補漲機率
4. 中國經濟數據對台灣出口的影響

以 JSON 回答：
{
  "opinion": "bullish",
  "confidence": 0.65,
  "fed_impact": "降息預期升溫，利多科技股",
  "currency_impact": "台幣升值壓力，注意匯損",
  "sox_impact": "費半突破新高，台半導體落後補漲機率高",
  "china_impact": "中國需求回溫，出口股受惠",
  "summary": "一句話總結（20字以內）"
}
opinion 只能是 bullish / neutral / bearish"""


async def run(stock_id: str = "", sector: str = "") -> AgentVote:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not api_key:
        return _fallback_macro_vote()

    try:
        import json
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": _MACRO_PROMPT}],
        )
        raw = msg.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        data = json.loads(raw)

        opinion = data.get("opinion", "neutral")
        conf    = float(data.get("confidence", 0.55))
        reasons = [
            data.get("fed_impact", ""),
            data.get("currency_impact", ""),
            data.get("sox_impact", ""),
        ]
        reasons = [r for r in reasons if r]

        return AgentVote(AGENT_NAME, opinion, conf, reasons, data_quality=0.8)

    except Exception as e:
        logger.warning("[MacroAgent] Claude failed: %s", e)
        return _fallback_macro_vote()


def _fallback_macro_vote() -> AgentVote:
    return AgentVote(
        AGENT_NAME, "neutral", 0.5,
        ["Fed 動向觀察中", "費半與台半導體連動中", "匯率波動注意"],
        data_quality=0.4,
    )


async def generate_weekly_report() -> str:
    vote = await run()
    icon = vote.OPINION_ICONS.get(vote.opinion, "⚪")
    lines = [
        "🌍 總經週報",
        "",
        f"整體研判：{icon} {vote.opinion_zh}（信心 {vote.confidence:.0%}）",
        "",
    ]
    for r in vote.reasons:
        lines.append(f"  • {r}")
    return "\n".join(lines)
