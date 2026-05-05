"""
narrative_os.py — 市場主線敘事追蹤系統

每日整合五大來源，輸出市場在炒什麼：
  1. YouTube 分析師話題頻率
  2. 新聞關鍵字出現次數
  3. 各族群成交量異常
  4. 外資買超族群
  5. 分析師共識分數
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

STAGE_ICONS = {
    "ACCUMULATION": "⚓",
    "AWARENESS":    "👁",
    "BREAKOUT":     "🚀",
    "MOMENTUM":     "🔥",
    "EUPHORIA":     "💥",
    "DISTRIBUTION": "⚠️",
    "DEAD":         "❄️",
}

TREND_ICONS = {
    "↑↑": "快速升溫",
    "↑":  "上升",
    "→":  "持平",
    "↓":  "降溫",
    "↓↓": "快速退燒",
}

KNOWN_NARRATIVES = [
    "AI Server", "半導體", "散熱", "PCB", "CoWoS", "HBM",
    "機器人", "電動車", "電源管理", "被動元件", "蘋果供應鏈",
    "存股ETF", "金融股", "航運", "面板", "工業電腦",
]


@dataclass
class NarrativeScore:
    name:           str
    score:          float       # 0-100
    trend:          str         # ↑↑ / ↑ / → / ↓ / ↓↓
    stage:          str         # ACCUMULATION..DEAD
    prev_score:     float = 0.0
    yt_mentions:    int   = 0
    news_mentions:  int   = 0
    volume_signal:  float = 0.0  # 0-1
    foreign_signal: float = 0.0  # 0-1
    consensus_score: float = 0.0
    top_stocks:     list[str] = field(default_factory=list)
    key_thesis:     str = ""

    @property
    def icon(self) -> str:
        return STAGE_ICONS.get(self.stage, "📊")

    @property
    def trend_desc(self) -> str:
        return TREND_ICONS.get(self.trend, "")

    def to_dict(self) -> dict:
        return {
            "name":            self.name,
            "score":           round(self.score, 1),
            "trend":           self.trend,
            "stage":           self.stage,
            "prev_score":      round(self.prev_score, 1),
            "yt_mentions":     self.yt_mentions,
            "news_mentions":   self.news_mentions,
            "volume_signal":   round(self.volume_signal, 3),
            "foreign_signal":  round(self.foreign_signal, 3),
            "consensus_score": round(self.consensus_score, 1),
            "top_stocks":      self.top_stocks,
            "key_thesis":      self.key_thesis,
        }


@dataclass
class NarrativeHeatmap:
    narratives:     list[NarrativeScore]
    top_narrative:  str
    rising_fast:    list[str]    # 快速崛起
    cooling_down:   list[str]    # 退燒中
    dead:           list[str]    # 已結束
    weekly_thesis:  str          # 本週核心敘事
    ts:             str = field(default_factory=lambda: datetime.now().isoformat())

    def format_line(self) -> str:
        hot   = [n for n in self.narratives if n.score >= 80]
        rise  = [n for n in self.narratives if n.trend in ("↑↑", "↑") and n.score < 80 and n.score >= 50]
        cool  = [n for n in self.narratives if n.trend in ("↓", "↓↓") and n.score >= 40]
        dead  = [n for n in self.narratives if n.stage == "DEAD" or n.score < 25]

        lines = ["🗺️ 市場敘事地圖", ""]
        for n in hot[:3]:
            lines.append(f"🔥 當前主線：{n.name}（{n.score:.0f}分）{n.trend}")
        for n in rise[:2]:
            lines.append(f"🚀 崛起主線：{n.name}（{n.score:.0f}分）快速升溫")
        for n in cool[:2]:
            lines.append(f"⚠️ 退燒中：{n.name}（{n.score:.0f}分）熱度下滑")
        for n in dead[:1]:
            lines.append(f"❄️ 結束：{n.name}（{n.score:.0f}分）資金撤離")

        if self.weekly_thesis:
            lines += ["", "本週核心敘事：", self.weekly_thesis]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "narratives":    [n.to_dict() for n in self.narratives],
            "top_narrative": self.top_narrative,
            "rising_fast":   self.rising_fast,
            "cooling_down":  self.cooling_down,
            "weekly_thesis": self.weekly_thesis,
            "ts":            self.ts,
        }


def _calc_trend(curr: float, prev: float) -> str:
    d = curr - prev
    if d > 15:  return "↑↑"
    if d > 5:   return "↑"
    if d < -15: return "↓↓"
    if d < -5:  return "↓"
    return "→"


def _score_to_stage(score: float, trend: str) -> str:
    if score < 25:
        return "DEAD"
    if score < 40:
        return "ACCUMULATION"
    if score < 55 and trend in ("↑", "↑↑"):
        return "AWARENESS"
    if score < 70 and trend in ("↑", "↑↑"):
        return "BREAKOUT"
    if score >= 70 and trend not in ("↓", "↓↓"):
        return "MOMENTUM"
    if score >= 80 and trend in ("↓", "↓↓"):
        return "DISTRIBUTION"
    return "AWARENESS"


async def _fetch_yt_mentions() -> dict[str, int]:
    """從 AnalystTopicStats 取近 7 日話題頻率"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystTopicStats
        from sqlalchemy import select, func
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(AnalystTopicStats.topic, func.sum(AnalystTopicStats.mention_count))
                .group_by(AnalystTopicStats.topic)
            )
            return {row[0]: int(row[1] or 0) for row in r.all()}
    except Exception:
        return {}


async def _fetch_news_mentions(days: int = 7) -> dict[str, int]:
    """從 NewsArticle 取近 N 日關鍵字出現次數"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import NewsArticle
        from sqlalchemy import select
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(NewsArticle.content, NewsArticle.title)
                .where(NewsArticle.published_at >= cutoff)
            )
            rows = r.all()

        counts: dict[str, int] = {}
        NARRATIVE_KEYWORDS = {
            "AI Server":   ["ai server", "ai伺服器", "算力", "輝達", "nvidia"],
            "半導體":       ["半導體", "晶圓", "tsmc", "台積電"],
            "散熱":         ["散熱", "液冷", "熱管"],
            "PCB":          ["pcb", "基板", "abf"],
            "CoWoS":        ["cowos", "先進封裝"],
            "機器人":        ["機器人", "robot", "人形機器人"],
            "電動車":        ["電動車", "ev", "tesla"],
            "被動元件":      ["被動元件", "mlcc", "電容"],
        }
        for title, content in rows:
            text = f"{title} {content}".lower()
            for narrative, keywords in NARRATIVE_KEYWORDS.items():
                if any(kw in text for kw in keywords):
                    counts[narrative] = counts.get(narrative, 0) + 1
        return counts
    except Exception:
        return {}


async def _fetch_consensus_scores() -> dict[str, float]:
    """從 AnalystConsensusDaily 取最新各股共識分數，再映射到敘事"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystConsensusDaily
        from sqlalchemy import select, func
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(AnalystConsensusDaily.stock_id,
                       func.avg(AnalystConsensusDaily.consensus_score))
                .where(AnalystConsensusDaily.date >= cutoff)
                .group_by(AnalystConsensusDaily.stock_id)
            )
            return {row[0]: float(row[1] or 0) for row in r.all()}
    except Exception:
        return {}


async def compute_narrative_heatmap() -> NarrativeHeatmap:
    """主函數：整合所有資料來源，計算敘事熱度地圖"""
    yt_mentions   = await _fetch_yt_mentions()
    news_mentions = await _fetch_news_mentions()
    consensus_map = await _fetch_consensus_scores()

    # 取前一日分數（若 DB 有記錄）
    prev_scores: dict[str, float] = {}
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import NarrativeLog
        from sqlalchemy import select
        cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(NarrativeLog).where(NarrativeLog.date >= cutoff)
                .order_by(NarrativeLog.date.desc())
            )
            for row in r.scalars().all():
                if row.narrative not in prev_scores:
                    prev_scores[row.narrative] = row.score
    except Exception:
        pass

    scores: list[NarrativeScore] = []
    max_yt   = max(yt_mentions.values(),   default=1) or 1
    max_news = max(news_mentions.values(), default=1) or 1

    for name in KNOWN_NARRATIVES:
        yt_n   = yt_mentions.get(name, 0)
        news_n = news_mentions.get(name, 0)

        yt_score   = (yt_n / max_yt)     * 40
        news_score = (news_n / max_news) * 30
        cons_score = min(consensus_map.get(name, 50) / 100 * 20, 20)
        vol_score  = 10   # placeholder（可從 sector rotation 取）

        total = yt_score + news_score + cons_score + vol_score
        prev  = prev_scores.get(name, total)
        trend = _calc_trend(total, prev)
        stage = _score_to_stage(total, trend)

        scores.append(NarrativeScore(
            name           = name,
            score          = round(total, 1),
            trend          = trend,
            stage          = stage,
            prev_score     = round(prev, 1),
            yt_mentions    = yt_n,
            news_mentions  = news_n,
            consensus_score = cons_score * 5,
        ))

    scores.sort(key=lambda s: -s.score)

    top    = scores[0].name if scores else ""
    rising = [s.name for s in scores if s.trend in ("↑↑",) and s.score < 80][:3]
    cool   = [s.name for s in scores if s.trend in ("↓↓",)][:3]
    dead   = [s.name for s in scores if s.stage == "DEAD"]

    # 生成本週核心敘事
    top2 = scores[:2]
    thesis = ""
    if len(top2) >= 2:
        thesis = (f"{top2[0].name}需求持續 → "
                  f"{top2[1].name if top2[1].name != top2[0].name else '相關族群'}受惠")

    heatmap = NarrativeHeatmap(
        narratives    = scores,
        top_narrative = top,
        rising_fast   = rising,
        cooling_down  = cool,
        dead          = [s.name for s in scores if s.stage == "DEAD"],
        weekly_thesis = thesis,
    )

    # 儲存到 DB
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import NarrativeLog
        today = datetime.now().strftime("%Y-%m-%d")
        async with AsyncSessionLocal() as db:
            for s in scores:
                db.add(NarrativeLog(
                    date      = today,
                    narrative = s.name,
                    score     = s.score,
                    trend     = s.trend,
                    stage     = s.stage,
                ))
            await db.commit()
    except Exception:
        pass

    return heatmap
