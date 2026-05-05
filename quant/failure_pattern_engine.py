"""
failure_pattern_engine.py — 失敗模式分析引擎

分析歷史交易記錄，找出高失敗率的操作模式，
在用戶即將重蹈覆轍時即時警告。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── 已知高失敗率模式（全市場統計）──────────────────────────────────────────────
GLOBAL_FAILURE_PATTERNS: list[dict] = [
    {"name": "高熱度追價",   "condition": "euphoria>80",      "win_rate": 0.31,
     "description": "Euphoria 超過 80 時追進"},
    {"name": "法人背離",     "condition": "inst_selling",     "win_rate": 0.28,
     "description": "買進時法人正在賣超"},
    {"name": "財報前追高",   "condition": "pre_earnings_high","win_rate": 0.35,
     "description": "財報公布前5天股價已漲超10%"},
    {"name": "逆勢買進",     "condition": "bearish_market",   "win_rate": 0.24,
     "description": "大盤下跌趨勢中逆勢買進"},
    {"name": "止損後重買",   "condition": "rebuy_after_stop", "win_rate": 0.22,
     "description": "觸碰停損賣出後24小時內重新買入"},
    {"name": "高乖離追漲",   "condition": "ma20_dist>15pct",  "win_rate": 0.33,
     "description": "股價偏離MA20超過15%時追漲"},
    {"name": "爆量追高",     "condition": "volume_spike_3x",  "win_rate": 0.38,
     "description": "成交量爆增3倍以上隔日追進"},
    {"name": "分析師強推後", "condition": "post_recommendation","win_rate": 0.41,
     "description": "YouTube 分析師強力推薦後第一天追買"},
]


@dataclass
class PatternWarning:
    pattern_name:    str
    global_win_rate: float
    user_win_rate:   Optional[float]
    user_occurrences: int
    triggered_count: int    # 此模式觸發次數（用戶個人）
    severity:        str    # HIGH / MEDIUM / LOW
    advice:          str

    def format_line(self) -> str:
        user_stat = (f"\n你個人紀錄：\n過去{self.user_occurrences}次此操作，"
                     f"虧損{self.triggered_count}次")  if self.user_occurrences else ""
        return (
            f"⚠️ 失敗模式警告\n\n"
            f"模式：{self.pattern_name}\n"
            f"歷史數據：此操作勝率只有{self.global_win_rate:.0%}"
            f"{user_stat}\n\n"
            f"建議：{self.advice}"
        )

    def to_dict(self) -> dict:
        return {
            "pattern":         self.pattern_name,
            "global_win_rate": round(self.global_win_rate, 3),
            "user_win_rate":   round(self.user_win_rate, 3) if self.user_win_rate else None,
            "severity":        self.severity,
            "advice":          self.advice,
        }


@dataclass
class FailureProfile:
    user_id:       str
    patterns:      list[dict] = field(default_factory=list)  # [{name, count, win_rate}]
    worst_pattern: str = ""
    total_trades:  int = 0
    overall_win_rate: float = 0.5

    def top_failures(self, n: int = 3) -> list[dict]:
        return sorted(self.patterns, key=lambda p: p.get("win_rate", 1.0))[:n]

    def format_line(self) -> str:
        if not self.patterns:
            return f"✅ {self.user_id} 目前無顯著失敗模式"
        lines = [f"📊 {self.user_id} 個人失敗模式檔案", ""]
        for p in self.top_failures(3):
            lines.append(f"  ⚠️ {p['name']}：{p['count']}次  勝率{p.get('win_rate', 0):.0%}")
        if self.worst_pattern:
            lines.append(f"\n最需改善：{self.worst_pattern}")
        return "\n".join(lines)


async def build_failure_profile(user_id: str) -> FailureProfile:
    """從用戶歷史交易記錄建立失敗模式檔案"""
    profile = FailureProfile(user_id=user_id)
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import TradeJournal
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(TradeJournal)
                .where(TradeJournal.user_id == user_id)
                .order_by(TradeJournal.date.desc())
                .limit(100)
            )
            trades = r.scalars().all()

        if not trades:
            return profile

        profile.total_trades = len(trades)
        wins = sum(1 for t in trades if (getattr(t, "pnl", 0) or 0) > 0)
        profile.overall_win_rate = wins / len(trades) if trades else 0.5

        # 分析各模式出現次數
        pattern_data: dict[str, list[float]] = {}
        for trade in trades:
            tags = getattr(trade, "tags", "") or ""
            for pat in GLOBAL_FAILURE_PATTERNS:
                cond = pat["condition"]
                if cond in tags.lower():
                    if pat["name"] not in pattern_data:
                        pattern_data[pat["name"]] = []
                    pnl = getattr(trade, "pnl", 0) or 0
                    pattern_data[pat["name"]].append(1.0 if pnl > 0 else 0.0)

        for name, results in pattern_data.items():
            win_rate = sum(results) / len(results)
            profile.patterns.append({
                "name":     name,
                "count":    len(results),
                "win_rate": win_rate,
            })

        if profile.patterns:
            worst = min(profile.patterns, key=lambda p: p["win_rate"])
            profile.worst_pattern = worst["name"]

    except Exception as e:
        logger.warning("[failure] build_profile failed for %s: %s", user_id, e)

    return profile


async def check_trade_warnings(
    user_id:   str,
    stock_id:  str,
    euphoria:  float = 50.0,
    inst_selling: bool = False,
    pre_earnings_high: bool = False,
    market_bearish: bool = False,
    just_stopped_out: bool = False,
    ma20_distance: float = 0.0,
    volume_spike_ratio: float = 1.0,
) -> list[PatternWarning]:
    """在用戶準備買進時，檢查是否觸發失敗模式"""
    warnings: list[PatternWarning] = []
    profile = await build_failure_profile(user_id)

    def _find_user_stat(pattern_name: str) -> tuple[int, Optional[float]]:
        for p in profile.patterns:
            if p["name"] == pattern_name:
                return p["count"], p["win_rate"]
        return 0, None

    checks = [
        (euphoria > 80,         "高熱度追價",   0.31, "等回調至 MA20 再評估"),
        (inst_selling,          "法人背離",     0.28, "等法人轉為買超後再進場"),
        (pre_earnings_high,     "財報前追高",   0.35, "財報後再確認方向"),
        (market_bearish,        "逆勢買進",     0.24, "等大盤止跌再行動"),
        (just_stopped_out,      "止損後重買",   0.22, "至少等24小時冷靜期"),
        (ma20_distance > 0.15,  "高乖離追漲",   0.33, "等股價回測 MA20 支撐"),
        (volume_spike_ratio > 3,"爆量追高",     0.38, "爆量日通常隔日回測，等確認"),
    ]

    for triggered, name, gwr, advice in checks:
        if not triggered:
            continue
        count, uwr = _find_user_stat(name)
        severity = "HIGH" if gwr < 0.30 else ("MEDIUM" if gwr < 0.40 else "LOW")
        warnings.append(PatternWarning(
            pattern_name     = name,
            global_win_rate  = gwr,
            user_win_rate    = uwr,
            user_occurrences = count,
            triggered_count  = int(count * (1 - (uwr or gwr))),
            severity         = severity,
            advice           = advice,
        ))

    warnings.sort(key=lambda w: w.global_win_rate)
    return warnings


def format_warnings_for_line(
    warnings: list[PatternWarning],
    stock_id: str,
    stock_name: str = "",
) -> str:
    if not warnings:
        return ""
    top = warnings[0]
    lines = [
        f"⚠️ 失敗模式警告",
        f"",
        f"你即將買進 {stock_id} {stock_name}",
        top.format_line().split("\n\n", 1)[1] if "\n\n" in top.format_line() else "",
    ]
    if len(warnings) > 1:
        lines.append(f"\n另有 {len(warnings)-1} 個次要警告")
    return "\n".join(lines)
