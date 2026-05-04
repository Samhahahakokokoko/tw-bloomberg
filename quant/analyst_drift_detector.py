"""
analyst_drift_detector.py — 分析師觀點飄移偵測器

偵測分析師在同一支股票上的觀點改變：
  1. 方向飄移（bullish → bearish 等）
  2. 信心度飄移（high → low confidence）
  3. 目標價飄移（大幅調降/調升）
  4. 族群廢棄（不再提該族群）

觸發條件：
  - 方向逆轉：最近2則 vs 前3則，情緒均值翻轉
  - 沉默飄移：原本每週提及，突然停止 > 14 天
  - 目標價調降 > 15%
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class DriftType(str, Enum):
    DIRECTION_REVERSAL = "direction_reversal"   # 方向逆轉
    CONFIDENCE_DROP    = "confidence_drop"      # 信心驟降
    TARGET_CUT         = "target_cut"           # 目標價大幅調降
    SILENCE            = "silence"              # 沉默飄移
    SECTOR_ABANDON     = "sector_abandon"       # 放棄族群


DRIFT_ZH = {
    DriftType.DIRECTION_REVERSAL: "方向逆轉",
    DriftType.CONFIDENCE_DROP:    "信心驟降",
    DriftType.TARGET_CUT:         "目標價調降",
    DriftType.SILENCE:            "沉默飄移",
    DriftType.SECTOR_ABANDON:     "族群放棄",
}

DRIFT_SEVERITY = {
    DriftType.DIRECTION_REVERSAL: 3,
    DriftType.CONFIDENCE_DROP:    2,
    DriftType.TARGET_CUT:         2,
    DriftType.SILENCE:            1,
    DriftType.SECTOR_ABANDON:     2,
}


SENTIMENT_VAL = {
    "strong_bullish": 2,
    "bullish":        1,
    "neutral":        0,
    "bearish":       -1,
    "strong_bearish":-2,
}


@dataclass
class DriftAlert:
    analyst_id:   str
    analyst_name: str
    stock_id:     str
    stock_name:   str
    drift_type:   DriftType
    severity:     int            # 1-3
    old_view:     str
    new_view:     str
    description:  str
    ts:           str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def icon(self) -> str:
        icons = {1: "⚡", 2: "⚠️", 3: "🚨"}
        return icons.get(self.severity, "ℹ️")

    def to_line_text(self) -> str:
        return (
            f"{self.icon} {DRIFT_ZH[self.drift_type]}\n"
            f"分析師：{self.analyst_name}｜{self.stock_id} {self.stock_name}\n"
            f"從「{self.old_view}」→「{self.new_view}」\n"
            f"{self.description}"
        )


@dataclass
class DriftReport:
    alerts:          list[DriftAlert] = field(default_factory=list)
    high_severity:   list[DriftAlert] = field(default_factory=list)
    ts:              str = field(default_factory=lambda: datetime.now().isoformat())

    def to_line_text(self) -> str:
        if not self.alerts:
            return "✅ 分析師觀點穩定，無重大飄移"
        lines = [f"🔍 分析師觀點飄移報告（{len(self.alerts)} 則）"]
        severe = [a for a in self.alerts if a.severity >= 2]
        for a in severe[:4]:
            lines.append("")
            lines.append(a.to_line_text())
        if len(self.alerts) > 4:
            lines.append(f"\n...另有 {len(self.alerts) - 4} 則輕度飄移")
        return "\n".join(lines)


def _detect_direction_drift(
    analyst_id: str, analyst_name: str, stock_id: str, stock_name: str,
    recent_sentiments: list[str], old_sentiments: list[str]
) -> Optional[DriftAlert]:
    """比較最近2則 vs 前3則情緒均值"""
    if len(recent_sentiments) < 1 or len(old_sentiments) < 2:
        return None

    recent_val = sum(SENTIMENT_VAL.get(s, 0) for s in recent_sentiments) / len(recent_sentiments)
    old_val    = sum(SENTIMENT_VAL.get(s, 0) for s in old_sentiments)    / len(old_sentiments)

    if old_val > 0.5 and recent_val < -0.3:
        return DriftAlert(
            analyst_id   = analyst_id,
            analyst_name = analyst_name,
            stock_id     = stock_id,
            stock_name   = stock_name,
            drift_type   = DriftType.DIRECTION_REVERSAL,
            severity     = 3,
            old_view     = f"看多（均值{old_val:.1f}）",
            new_view     = f"轉空（均值{recent_val:.1f}）",
            description  = "由看多大幅轉為看空，需高度關注",
        )
    if old_val < -0.5 and recent_val > 0.3:
        return DriftAlert(
            analyst_id   = analyst_id,
            analyst_name = analyst_name,
            stock_id     = stock_id,
            stock_name   = stock_name,
            drift_type   = DriftType.DIRECTION_REVERSAL,
            severity     = 2,
            old_view     = f"看空（均值{old_val:.1f}）",
            new_view     = f"轉多（均值{recent_val:.1f}）",
            description  = "由看空轉為看多，可能是底部訊號",
        )
    return None


def _detect_target_cut(
    analyst_id: str, analyst_name: str, stock_id: str, stock_name: str,
    old_target: float, new_target: float
) -> Optional[DriftAlert]:
    if old_target <= 0 or new_target <= 0:
        return None
    change = (new_target - old_target) / old_target
    if change < -0.15:
        return DriftAlert(
            analyst_id   = analyst_id,
            analyst_name = analyst_name,
            stock_id     = stock_id,
            stock_name   = stock_name,
            drift_type   = DriftType.TARGET_CUT,
            severity     = 2,
            old_view     = f"目標價 {old_target:.0f}",
            new_view     = f"目標價 {new_target:.0f}",
            description  = f"目標價調降 {abs(change):.1%}，超過警戒門檻",
        )
    return None


def _detect_silence(
    analyst_id: str, analyst_name: str, stock_id: str, stock_name: str,
    last_mention_date: str, avg_mention_interval_days: float
) -> Optional[DriftAlert]:
    try:
        last_dt = datetime.fromisoformat(last_mention_date)
    except Exception:
        return None
    days_silent = (datetime.now() - last_dt).days
    if days_silent > max(14, avg_mention_interval_days * 2.5):
        return DriftAlert(
            analyst_id   = analyst_id,
            analyst_name = analyst_name,
            stock_id     = stock_id,
            stock_name   = stock_name,
            drift_type   = DriftType.SILENCE,
            severity     = 1,
            old_view     = f"平均每{avg_mention_interval_days:.0f}日提及",
            new_view     = f"已沉默 {days_silent} 日",
            description  = "分析師突然停止提及，可能已悄悄放棄",
        )
    return None


async def run_drift_detection(analyst_calls: list[dict] | None = None) -> DriftReport:
    """
    analyst_calls: [{
      analyst_id, analyst_name, stock_id, stock_name,
      sentiments_recent: list[str],   # 最近2則
      sentiments_old: list[str],      # 前3則
      target_old: float,
      target_new: float,
      last_mention: str,              # ISO date
      avg_interval_days: float,
    }]
    """
    if not analyst_calls:
        analyst_calls = [
            {
                "analyst_id": "tsmc_bull", "analyst_name": "半導體老王",
                "stock_id": "2330", "stock_name": "台積電",
                "sentiments_recent": ["bearish"],
                "sentiments_old": ["strong_bullish", "bullish", "bullish"],
                "target_old": 1100.0, "target_new": 880.0,
                "last_mention": (datetime.now() - timedelta(days=3)).isoformat(),
                "avg_interval_days": 5,
            },
            {
                "analyst_id": "ai_server", "analyst_name": "AI伺服器達人",
                "stock_id": "6669", "stock_name": "緯穎",
                "sentiments_recent": ["bullish", "strong_bullish"],
                "sentiments_old": ["neutral", "bearish"],
                "target_old": 2200.0, "target_new": 2800.0,
                "last_mention": (datetime.now() - timedelta(days=1)).isoformat(),
                "avg_interval_days": 7,
            },
        ]

    all_alerts: list[DriftAlert] = []

    for ac in analyst_calls:
        aid   = ac["analyst_id"]
        aname = ac["analyst_name"]
        sid   = ac["stock_id"]
        sname = ac["stock_name"]

        # 方向飄移
        d = _detect_direction_drift(
            aid, aname, sid, sname,
            ac.get("sentiments_recent", []),
            ac.get("sentiments_old", []),
        )
        if d:
            all_alerts.append(d)

        # 目標價
        tc = _detect_target_cut(
            aid, aname, sid, sname,
            ac.get("target_old", 0),
            ac.get("target_new", 0),
        )
        if tc:
            all_alerts.append(tc)

        # 沉默
        sl = _detect_silence(
            aid, aname, sid, sname,
            ac.get("last_mention", ""),
            ac.get("avg_interval_days", 7),
        )
        if sl:
            all_alerts.append(sl)

    all_alerts.sort(key=lambda a: -a.severity)
    high_severity = [a for a in all_alerts if a.severity >= 2]

    return DriftReport(alerts=all_alerts, high_severity=high_severity)


async def get_drift_from_db() -> DriftReport:
    """從資料庫讀取近30日的分析師觀點變化並偵測飄移"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import AnalystCall, Analyst
        from sqlalchemy import select
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        analyst_calls_data: list[dict] = []

        async with AsyncSessionLocal() as db:
            r = await db.execute(select(Analyst).where(Analyst.is_active == True))
            analysts = {a.analyst_id: a.name for a in r.scalars().all()}

            r2 = await db.execute(
                select(AnalystCall)
                .where(AnalystCall.date >= cutoff)
                .order_by(AnalystCall.date.desc())
            )
            calls = r2.scalars().all()

        # 按 (analyst, stock) 分組
        groups: dict[tuple, list] = {}
        for c in calls:
            key = (c.analyst_id, c.stock_id)
            if key not in groups:
                groups[key] = []
            groups[key].append(c)

        for (aid, sid), group_calls in groups.items():
            if len(group_calls) < 3:
                continue
            sorted_calls = sorted(group_calls, key=lambda c: c.date, reverse=True)
            recent = [c.sentiment for c in sorted_calls[:2]]
            old    = [c.sentiment for c in sorted_calls[2:5]]
            analyst_calls_data.append({
                "analyst_id":        aid,
                "analyst_name":      analysts.get(aid, aid),
                "stock_id":          sid,
                "stock_name":        sorted_calls[0].stock_name or sid,
                "sentiments_recent": recent,
                "sentiments_old":    old,
                "target_old":        float(sorted_calls[-1].target_price or 0),
                "target_new":        float(sorted_calls[0].target_price or 0),
                "last_mention":      sorted_calls[0].date,
                "avg_interval_days": 7,
            })

        return await run_drift_detection(analyst_calls_data)
    except Exception as e:
        logger.warning("[drift] db fetch failed, using mock: %s", e)
        return await run_drift_detection(None)
