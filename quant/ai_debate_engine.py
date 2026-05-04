"""
ai_debate_engine.py — Claude AI 多空辯論引擎

讓 Claude 同時扮演多方和空方分析師進行辯論：
  1. 多方立場：找5個看多理由
  2. 空方立場：找5個看空理由
  3. 裁判仲裁：綜合評分給出最終建議

使用 claude-haiku 以降低成本
結果格式：
  - bull_points: list[str]
  - bear_points: list[str]
  - verdict: str
  - confidence: float
  - recommendation: "BUY" / "HOLD" / "SELL"
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class DebateResult:
    stock_id:       str
    stock_name:     str
    bull_points:    list[str]
    bear_points:    list[str]
    verdict:        str
    confidence:     float     # 0-100
    recommendation: str       # BUY / HOLD / SELL
    used_ai:        bool = True
    ts:             str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def rec_emoji(self) -> str:
        return {"BUY": "🟢", "HOLD": "🟡", "SELL": "🔴"}.get(self.recommendation, "⚪")

    def to_line_text(self) -> str:
        lines = [
            f"⚔️ AI 多空辯論｜{self.stock_id} {self.stock_name}",
            "",
            "🟢 多方論點：",
        ]
        for i, pt in enumerate(self.bull_points[:3], 1):
            lines.append(f"  {i}. {pt}")
        lines.append("")
        lines.append("🔴 空方論點：")
        for i, pt in enumerate(self.bear_points[:3], 1):
            lines.append(f"  {i}. {pt}")
        lines.append("")
        lines.append(f"⚖️ 裁決：{self.verdict}")
        lines.append(f"{self.rec_emoji} 建議：{self.recommendation}  信心 {self.confidence:.0f}%")
        return "\n".join(lines)


_DEBATE_PROMPT = """你是一個專業的台股分析系統，請對以下股票進行多空辯論分析。

股票：{stock_id} {stock_name}
近期資訊：{context}

請用繁體中文以 JSON 格式回答，格式如下：
{{
  "bull_points": ["看多理由1", "看多理由2", "看多理由3", "看多理由4", "看多理由5"],
  "bear_points": ["看空理由1", "看空理由2", "看空理由3", "看空理由4", "看空理由5"],
  "verdict": "一句話綜合裁決（20字以內）",
  "confidence": 65,
  "recommendation": "BUY"
}}

recommendation 只能是 BUY / HOLD / SELL 其中之一。
confidence 是你對此建議的信心度（0-100）。
保持客觀，多空論點都要有實質內容，不能敷衍。"""


_FALLBACK_DEBATES: dict[str, dict] = {
    "default": {
        "bull_points": [
            "AI資本支出週期持續擴大，供應鏈受惠明確",
            "外資持續加碼，籌碼面健康",
            "毛利率改善趨勢確立，獲利能見度高",
            "技術面突破關鍵壓力區，動能強勁",
            "產業景氣上行，訂單能見度延伸至下半年",
        ],
        "bear_points": [
            "本益比已達歷史高位，安全邊際偏低",
            "美中貿易摩擦持續，地緣政治風險未消",
            "利率環境仍偏高，估值面承壓",
            "競爭對手積極擴產，毛利率長期恐受壓",
            "融資比例偏高，短線有斷頭風險",
        ],
        "verdict": "多頭氣勢佔優，但估值偏高需謹慎",
        "confidence": 62,
        "recommendation": "HOLD",
    }
}


async def run_ai_debate(
    stock_id: str,
    stock_name: str,
    context: str = "",
) -> DebateResult:
    """呼叫 Claude API 進行多空辯論"""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not api_key:
        logger.info("[debate] no API key, using fallback for %s", stock_id)
        fb = _FALLBACK_DEBATES.get(stock_id, _FALLBACK_DEBATES["default"])
        return DebateResult(
            stock_id       = stock_id,
            stock_name     = stock_name,
            bull_points    = fb["bull_points"],
            bear_points    = fb["bear_points"],
            verdict        = fb["verdict"],
            confidence     = fb["confidence"],
            recommendation = fb["recommendation"],
            used_ai        = False,
        )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = _DEBATE_PROMPT.format(
            stock_id   = stock_id,
            stock_name = stock_name,
            context    = context or "無額外資訊，請根據一般市場知識分析",
        )

        msg = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 800,
            messages   = [{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()

        # 提取 JSON
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()

        data = json.loads(raw)

        return DebateResult(
            stock_id       = stock_id,
            stock_name     = stock_name,
            bull_points    = data.get("bull_points", [])[:5],
            bear_points    = data.get("bear_points", [])[:5],
            verdict        = data.get("verdict", "無裁決"),
            confidence     = float(data.get("confidence", 50)),
            recommendation = data.get("recommendation", "HOLD").upper(),
            used_ai        = True,
        )

    except Exception as e:
        logger.warning("[debate] Claude API failed for %s: %s", stock_id, e)
        fb = _FALLBACK_DEBATES.get(stock_id, _FALLBACK_DEBATES["default"])
        return DebateResult(
            stock_id       = stock_id,
            stock_name     = stock_name,
            bull_points    = fb["bull_points"],
            bear_points    = fb["bear_points"],
            verdict        = fb["verdict"],
            confidence     = fb["confidence"],
            recommendation = fb["recommendation"],
            used_ai        = False,
        )


async def batch_debate(stocks: list[dict]) -> list[DebateResult]:
    """批次辯論多支股票（避免 API 速率限制，每支最多間隔1秒）"""
    import asyncio
    results = []
    for s in stocks:
        r = await run_ai_debate(
            stock_id   = s.get("stock_id", ""),
            stock_name = s.get("stock_name", ""),
            context    = s.get("context", ""),
        )
        results.append(r)
        await asyncio.sleep(0.5)
    return results
