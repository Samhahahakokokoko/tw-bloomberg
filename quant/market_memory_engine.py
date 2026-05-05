"""
market_memory_engine.py — 歷史情境比對引擎

當日特徵向量 vs 歷史特徵向量，用 cosine similarity 找最相似的歷史時期。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── 預設歷史情境資料庫 ────────────────────────────────────────────────────────
# 特徵向量 [rsi_norm, foreign_flow, narrative_hot, euphoria, vol_ratio, breadth]
HISTORICAL_SCENARIOS: list[dict] = [
    {
        "period": "2023年3月",
        "label":  "AI Server 初期啟動",
        "features": [0.55, 0.70, 0.45, 0.40, 1.20, 0.55],
        "description": "AI Server 剛開始被市場認識，外資開始加碼半導體，散戶熱度還低（早期）",
        "outcome":     "台積電從550漲到750（+36%），散熱族群啟動主升段，持續約4個月",
        "implication": "如果走勢相似，現在可能仍在早期，關鍵確認點：外資期貨是否持續加多",
        "top_sectors": ["半導體", "AI Server", "散熱"],
    },
    {
        "period": "2023年7月",
        "label":  "AI Server 主升段高潮",
        "features": [0.82, 0.85, 0.90, 0.78, 2.50, 0.80],
        "description": "AI Server 全面爆發，散熱PCB大漲，外資瘋狂買進，散戶追高",
        "outcome":     "主升段結束後快速回調15-20%，高點追進者套牢",
        "implication": "當前可能接近高點，宜減碼鎖利",
        "top_sectors": ["散熱", "PCB", "AI Server"],
    },
    {
        "period": "2022年10月",
        "label":  "熊市底部反彈",
        "features": [0.25, 0.20, 0.15, 0.10, 0.60, 0.25],
        "description": "大盤連跌後出現低估值族群，外資輕量，散戶恐慌",
        "outcome":     "後續3個月反彈25%，存股ETF先動，電子後動",
        "implication": "低檔布局機會，首選防禦性標的",
        "top_sectors": ["存股ETF", "金融股", "電信"],
    },
    {
        "period": "2024年1月",
        "label":  "機器人題材初啟",
        "features": [0.58, 0.65, 0.52, 0.45, 1.35, 0.60],
        "description": "機器人、人形機器人開始出現在媒體，但市場認知度還不高",
        "outcome":     "持續6個月緩步上漲，初期進場報酬最高",
        "implication": "題材擴散初期，建議輕倉布局",
        "top_sectors": ["機器人", "感測器", "減速機"],
    },
    {
        "period": "2024年7月",
        "label":  "CoWoS 高峰退燒",
        "features": [0.75, 0.50, 0.85, 0.82, 2.20, 0.70],
        "description": "CoWoS 敘事達到最高熱度，分析師開始出現分歧",
        "outcome":     "CoWoS 相關股回調30%+，資金轉向下一個題材",
        "implication": "敘事過熱末期，轉換到下游未漲族群",
        "top_sectors": ["PCB", "電源管理", "被動元件"],
    },
]


@dataclass
class MemoryMatch:
    period:       str
    label:        str
    similarity:   float   # 0-1
    description:  str
    outcome:      str
    implication:  str
    top_sectors:  list[str] = field(default_factory=list)

    def format_line(self) -> str:
        sim_bar = "█" * int(self.similarity * 10) + "░" * (10 - int(self.similarity * 10))
        lines = [
            f"📚 市場記憶分析",
            f"",
            f"現在最像：{self.period} — {self.label}",
            f"相似度：[{sim_bar}] {self.similarity:.0%}",
            f"",
            f"當時特徵：",
            f"  {self.description}",
            f"",
            f"{self.period}之後發生了什麼：",
            f"  {self.outcome}",
            f"",
            f"現在的含義：",
            f"  {self.implication}",
        ]
        if self.top_sectors:
            lines.append(f"\n受惠族群：{'、'.join(self.top_sectors)}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "period":      self.period,
            "label":       self.label,
            "similarity":  round(self.similarity, 3),
            "description": self.description,
            "outcome":     self.outcome,
            "implication": self.implication,
            "top_sectors": self.top_sectors,
        }


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot  = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x ** 2 for x in a)) or 1e-9
    norm_b = math.sqrt(sum(x ** 2 for x in b)) or 1e-9
    return dot / (norm_a * norm_b)


async def _build_current_vector() -> list[float]:
    """
    建立當日特徵向量：
    [rsi_norm, foreign_flow, narrative_hot, euphoria, vol_ratio, breadth]
    每個維度正規化到 0-1
    """
    vec = [0.5, 0.5, 0.5, 0.5, 1.0, 0.5]   # default

    try:
        # RSI 正規化
        from backend.services.twse_service import fetch_market_overview
        mkt = await fetch_market_overview()
        if mkt and mkt.get("rsi"):
            vec[0] = min(1.0, max(0.0, float(mkt["rsi"]) / 100))
    except Exception:
        pass

    try:
        # Euphoria
        from quant.euphoria_engine import compute_euphoria
        eu = await compute_euphoria()
        vec[3] = eu.euphoria_score / 100
    except Exception:
        pass

    try:
        # Narrative heat（取最高分敘事）
        from quant.narrative_os import compute_narrative_heatmap
        hm = await compute_narrative_heatmap()
        if hm.narratives:
            vec[2] = hm.narratives[0].score / 100
    except Exception:
        pass

    return vec


async def find_similar_period(top_n: int = 3) -> list[MemoryMatch]:
    current_vec = await _build_current_vector()

    matches: list[MemoryMatch] = []
    for scenario in HISTORICAL_SCENARIOS:
        sim = _cosine_similarity(current_vec, scenario["features"])
        matches.append(MemoryMatch(
            period      = scenario["period"],
            label       = scenario["label"],
            similarity  = sim,
            description = scenario["description"],
            outcome     = scenario["outcome"],
            implication = scenario["implication"],
            top_sectors = scenario.get("top_sectors", []),
        ))

    matches.sort(key=lambda m: -m.similarity)
    return matches[:top_n]


async def get_best_match() -> Optional[MemoryMatch]:
    results = await find_similar_period(1)
    return results[0] if results else None
