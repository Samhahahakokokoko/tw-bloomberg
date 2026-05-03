"""Factor Exposure Dashboard — 分析持股的因子暴露度"""
from __future__ import annotations

from dataclasses import dataclass
from loguru import logger


SECTOR_FACTOR_MAP: dict[str, dict[str, float]] = {
    "半導體":      {"momentum": 0.8, "growth": 0.7, "value": 0.3, "ai_tech": 0.6},
    "IC設計":      {"momentum": 0.7, "growth": 0.8, "value": 0.2, "ai_tech": 0.7},
    "AI Server":   {"momentum": 0.9, "growth": 0.9, "value": 0.2, "ai_tech": 0.95},
    "伺服器":      {"momentum": 0.85,"growth": 0.85,"value": 0.25,"ai_tech": 0.9},
    "散熱/電源":   {"momentum": 0.8, "growth": 0.7, "value": 0.3, "ai_tech": 0.8},
    "PCB":         {"momentum": 0.6, "growth": 0.5, "value": 0.4, "ai_tech": 0.5},
    "晶圓代工":    {"momentum": 0.7, "growth": 0.6, "value": 0.4, "ai_tech": 0.6},
    "電動車":      {"momentum": 0.6, "growth": 0.7, "value": 0.3, "ai_tech": 0.5},
    "金融":        {"momentum": 0.3, "growth": 0.3, "value": 0.8, "ai_tech": 0.1},
    "傳產":        {"momentum": 0.3, "growth": 0.2, "value": 0.8, "ai_tech": 0.1},
    "航運":        {"momentum": 0.5, "growth": 0.3, "value": 0.5, "ai_tech": 0.1},
    "生技":        {"momentum": 0.5, "growth": 0.8, "value": 0.2, "ai_tech": 0.3},
    "電商":        {"momentum": 0.5, "growth": 0.6, "value": 0.3, "ai_tech": 0.5},
}
DEFAULT_FACTORS = {"momentum": 0.5, "growth": 0.5, "value": 0.5, "ai_tech": 0.3}


@dataclass
class FactorExposure:
    momentum:     float   # 0~1
    growth:       float
    value:        float
    ai_tech:      float
    concentration: float  # 族群集中度 0~1

    def to_line_text(self) -> str:
        def bar(v: float, width: int = 10) -> str:
            filled = round(v * width)
            return "█" * filled + "░" * (width - filled)

        warnings = []
        if self.ai_tech >= 0.8:
            warnings.append("AI/科技暴露偏高")
        if self.concentration >= 0.7:
            warnings.append("族群集中度偏高")
        if self.value < 0.3:
            warnings.append("價值因子不足，考慮增加防禦性股票")

        lines = [
            "📊 因子暴露分析",
            "─" * 20,
            "",
            "你的投組風格：",
            f"動能因子：{bar(self.momentum)} {self.momentum*100:.0f}%",
            f"成長因子：{bar(self.growth)} {self.growth*100:.0f}%",
            f"價值因子：{bar(self.value)} {self.value*100:.0f}%",
            f"AI暴露：  {bar(self.ai_tech)} {self.ai_tech*100:.0f}%"
            + ("（⚠️偏高）" if self.ai_tech >= 0.8 else ""),
            f"集中度：  {bar(self.concentration)} {self.concentration*100:.0f}%"
            + ("（⚠️分散不足）" if self.concentration >= 0.7 else ""),
        ]

        if warnings:
            lines.append("")
            lines.append("建議：" + "，".join(warnings))
        else:
            lines.append("")
            lines.append("建議：因子配置均衡，持續保持")

        return "\n".join(lines)


async def calculate_exposure(uid: str) -> FactorExposure:
    """計算用戶持股的因子暴露度"""
    exp = FactorExposure(
        momentum=0.5, growth=0.5, value=0.5,
        ai_tech=0.3, concentration=0.5,
    )
    try:
        from .portfolio_service import get_holdings
        holdings = await get_holdings(uid)
        if not holdings:
            return exp

        total_val = sum(h.get("market_value", 0) or 0 for h in holdings)
        if total_val == 0:
            return exp

        m_sum = g_sum = v_sum = a_sum = 0.0
        sector_weights: dict[str, float] = {}

        for h in holdings:
            val    = h.get("market_value", 0) or 0
            weight = val / total_val
            sector = h.get("sector", "其他")
            fmap   = SECTOR_FACTOR_MAP.get(sector, DEFAULT_FACTORS)

            m_sum += fmap["momentum"] * weight
            g_sum += fmap["growth"]   * weight
            v_sum += fmap["value"]    * weight
            a_sum += fmap["ai_tech"]  * weight

            sector_weights[sector] = sector_weights.get(sector, 0) + weight

        conc = max(sector_weights.values()) if sector_weights else 0.5

        exp = FactorExposure(
            momentum      = round(m_sum, 2),
            growth        = round(g_sum, 2),
            value         = round(v_sum, 2),
            ai_tech       = round(a_sum, 2),
            concentration = round(conc, 2),
        )
    except Exception as e:
        logger.error(f"[factor_exposure] calculate failed: {e}")

    return exp
