"""
timeline_engine.py — 六段市場週期辨識引擎

ACCUMULATION → AWARENESS → BREAKOUT → MOMENTUM → EUPHORIA → DISTRIBUTION

每段週期有不同的最佳策略：
  - ACCUMULATION: 低調建倉，等待訊號
  - AWARENESS:    早期布局，寬鬆止損
  - BREAKOUT:     突破確認，追漲
  - MOMENTUM:     強勢持有，移動止損
  - EUPHORIA:     減倉，鎖利
  - DISTRIBUTION: 清倉，反向信號
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class CycleStage(str, Enum):
    ACCUMULATION = "ACCUMULATION"
    AWARENESS    = "AWARENESS"
    BREAKOUT     = "BREAKOUT"
    MOMENTUM     = "MOMENTUM"
    EUPHORIA     = "EUPHORIA"
    DISTRIBUTION = "DISTRIBUTION"


STAGE_ZH = {
    CycleStage.ACCUMULATION: "底部築底",
    CycleStage.AWARENESS:    "醒覺啟動",
    CycleStage.BREAKOUT:     "突破確認",
    CycleStage.MOMENTUM:     "強勢動能",
    CycleStage.EUPHORIA:     "過熱泡沫",
    CycleStage.DISTRIBUTION: "高檔出貨",
}

STAGE_ACTION = {
    CycleStage.ACCUMULATION: "靜觀其變，小量試探",
    CycleStage.AWARENESS:    "早期布局，寬鬆止損",
    CycleStage.BREAKOUT:     "突破確認後追漲",
    CycleStage.MOMENTUM:     "強勢持有，移動止損",
    CycleStage.EUPHORIA:     "逐步減倉，鎖利出場",
    CycleStage.DISTRIBUTION: "清倉觀望，等待下一循環",
}

STAGE_EMOJI = {
    CycleStage.ACCUMULATION: "⚓",
    CycleStage.AWARENESS:    "👁",
    CycleStage.BREAKOUT:     "🚀",
    CycleStage.MOMENTUM:     "💨",
    CycleStage.EUPHORIA:     "🔥",
    CycleStage.DISTRIBUTION: "⚠️",
}


@dataclass
class StageSignal:
    name: str
    value: float     # 0-1 正規化
    bullish: bool    # True=支持更高週期
    weight: float = 1.0


@dataclass
class TimelineResult:
    stock_id:      str
    stock_name:    str
    stage:         CycleStage
    confidence:    float          # 0-100
    signals:       list[StageSignal] = field(default_factory=list)
    days_in_stage: int = 0
    next_stage:    Optional[CycleStage] = None
    risk_note:     str = ""
    ts:            str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def stage_zh(self) -> str:
        return STAGE_ZH.get(self.stage, self.stage.value)

    @property
    def action_hint(self) -> str:
        return STAGE_ACTION.get(self.stage, "")

    @property
    def emoji(self) -> str:
        return STAGE_EMOJI.get(self.stage, "")

    def to_line_text(self) -> str:
        bar = "█" * int(self.confidence / 10) + "░" * (10 - int(self.confidence / 10))
        lines = [
            f"{self.emoji} {self.stock_id} {self.stock_name}",
            f"週期：{self.stage_zh}（{self.stage.value}）",
            f"信心：{bar} {self.confidence:.0f}%",
            f"建議：{self.action_hint}",
        ]
        if self.days_in_stage:
            lines.append(f"持續：{self.days_in_stage} 個交易日")
        if self.risk_note:
            lines.append(f"⚠️ {self.risk_note}")
        return "\n".join(lines)


def _classify_stage(
    ret_5d: float,
    ret_20d: float,
    ret_60d: float,
    vol_ratio: float,
    rsi: float,
    above_ma20: bool,
    above_ma60: bool,
    foreign_5d: float,
    bb_position: float,  # 0=下軌, 1=上軌
) -> tuple[CycleStage, float, list[StageSignal]]:
    """根據技術指標組合判斷週期位置"""
    signals: list[StageSignal] = []
    stage_scores: dict[CycleStage, float] = {s: 0.0 for s in CycleStage}

    # ── RSI 信號 ──
    if rsi < 35:
        signals.append(StageSignal("RSI超賣", rsi / 100, False, 1.2))
        stage_scores[CycleStage.ACCUMULATION] += 2
    elif 35 <= rsi < 50:
        signals.append(StageSignal("RSI回溫", rsi / 100, True, 1.0))
        stage_scores[CycleStage.AWARENESS] += 1.5
    elif 50 <= rsi < 65:
        signals.append(StageSignal("RSI健康", rsi / 100, True, 1.0))
        stage_scores[CycleStage.BREAKOUT] += 1
        stage_scores[CycleStage.MOMENTUM] += 1
    elif 65 <= rsi < 80:
        signals.append(StageSignal("RSI強勢", rsi / 100, True, 0.8))
        stage_scores[CycleStage.MOMENTUM] += 1.5
    else:  # rsi >= 80
        signals.append(StageSignal("RSI過熱", rsi / 100, False, 1.5))
        stage_scores[CycleStage.EUPHORIA] += 2
        stage_scores[CycleStage.DISTRIBUTION] += 1

    # ── 均線位置 ──
    if not above_ma20 and not above_ma60:
        stage_scores[CycleStage.ACCUMULATION] += 1.5
        stage_scores[CycleStage.DISTRIBUTION] += 1
    elif above_ma20 and not above_ma60:
        signals.append(StageSignal("突破MA20", 0.6, True, 1.0))
        stage_scores[CycleStage.AWARENESS] += 1.5
        stage_scores[CycleStage.BREAKOUT] += 1
    elif above_ma20 and above_ma60:
        signals.append(StageSignal("站上MA60", 0.8, True, 1.2))
        stage_scores[CycleStage.BREAKOUT] += 1
        stage_scores[CycleStage.MOMENTUM] += 1.5

    # ── 成交量 ──
    if vol_ratio > 3.0:
        signals.append(StageSignal("爆量", vol_ratio / 5, False, 1.5))
        stage_scores[CycleStage.EUPHORIA] += 1.5
        stage_scores[CycleStage.DISTRIBUTION] += 1
    elif vol_ratio > 2.0:
        signals.append(StageSignal("大量", vol_ratio / 5, True, 1.0))
        stage_scores[CycleStage.BREAKOUT] += 1.5
        stage_scores[CycleStage.MOMENTUM] += 1
    elif vol_ratio > 1.3:
        signals.append(StageSignal("量增", vol_ratio / 5, True, 0.8))
        stage_scores[CycleStage.AWARENESS] += 1
        stage_scores[CycleStage.BREAKOUT] += 0.5
    elif vol_ratio < 0.7:
        signals.append(StageSignal("縮量", vol_ratio / 5, False, 0.8))
        stage_scores[CycleStage.ACCUMULATION] += 0.5

    # ── 報酬率 ──
    if ret_5d > 0.15:
        signals.append(StageSignal("5D暴漲", ret_5d, False, 1.5))
        stage_scores[CycleStage.EUPHORIA] += 2
    elif ret_5d > 0.05:
        stage_scores[CycleStage.BREAKOUT] += 1
        stage_scores[CycleStage.MOMENTUM] += 0.5
    elif ret_5d < -0.05:
        stage_scores[CycleStage.DISTRIBUTION] += 1
        stage_scores[CycleStage.ACCUMULATION] += 0.5

    if ret_60d > 0.40:
        signals.append(StageSignal("3M大漲", ret_60d, False, 1.0))
        stage_scores[CycleStage.EUPHORIA] += 1
        stage_scores[CycleStage.DISTRIBUTION] += 0.5

    # ── 法人動向 ──
    if foreign_5d > 0:
        signals.append(StageSignal("外資買超", min(foreign_5d / 10000, 1.0), True, 1.2))
        stage_scores[CycleStage.AWARENESS] += 0.5
        stage_scores[CycleStage.MOMENTUM] += 0.5
    elif foreign_5d < 0:
        signals.append(StageSignal("外資賣超", min(abs(foreign_5d) / 10000, 1.0), False, 1.0))
        stage_scores[CycleStage.DISTRIBUTION] += 1

    # ── Bollinger Band 位置 ──
    if bb_position > 0.9:
        signals.append(StageSignal("觸上軌", bb_position, False, 1.0))
        stage_scores[CycleStage.EUPHORIA] += 1
    elif bb_position < 0.1:
        signals.append(StageSignal("觸下軌", 1 - bb_position, True, 1.0))
        stage_scores[CycleStage.ACCUMULATION] += 1

    # 找最高分週期
    best_stage = max(stage_scores, key=lambda s: stage_scores[s])
    best_score = stage_scores[best_stage]
    total_score = sum(stage_scores.values()) or 1

    confidence = min(100.0, (best_score / total_score) * 250)
    return best_stage, confidence, signals


async def analyze_timeline(stock_data: dict) -> TimelineResult:
    """
    stock_data 格式：
    {
      stock_id, stock_name,
      ret_5d, ret_20d, ret_60d,
      vol_ratio, rsi, above_ma20, above_ma60,
      foreign_buy_5d, bb_position,
      days_in_stage (optional)
    }
    """
    sid = stock_data.get("stock_id", "UNKNOWN")
    sname = stock_data.get("stock_name", sid)

    stage, confidence, signals = _classify_stage(
        ret_5d     = stock_data.get("ret_5d", 0),
        ret_20d    = stock_data.get("ret_20d", 0),
        ret_60d    = stock_data.get("ret_60d", 0),
        vol_ratio  = stock_data.get("vol_ratio", 1.0),
        rsi        = stock_data.get("rsi", 50),
        above_ma20 = stock_data.get("above_ma20", True),
        above_ma60 = stock_data.get("above_ma60", True),
        foreign_5d = stock_data.get("foreign_buy_5d", 0),
        bb_position = stock_data.get("bb_position", 0.5),
    )

    # 風險提示
    risk_note = ""
    if stage == CycleStage.EUPHORIA and stock_data.get("vol_ratio", 1) > 3:
        risk_note = "爆量過熱，注意反轉"
    elif stage == CycleStage.DISTRIBUTION and stock_data.get("foreign_buy_5d", 0) < -5000:
        risk_note = "外資大幅賣超，確認出貨"

    stage_order = list(CycleStage)
    idx = stage_order.index(stage)
    next_stage = stage_order[idx + 1] if idx < len(stage_order) - 1 else None

    return TimelineResult(
        stock_id      = sid,
        stock_name    = sname,
        stage         = stage,
        confidence    = round(confidence, 1),
        signals       = signals,
        days_in_stage = stock_data.get("days_in_stage", 0),
        next_stage    = next_stage,
        risk_note     = risk_note,
    )


async def run_market_timeline(stocks: list[dict] | None = None) -> list[TimelineResult]:
    """掃描多支股票，回傳週期分布"""
    if not stocks:
        # Mock data for testing
        stocks = [
            {"stock_id": "2330", "stock_name": "台積電",  "ret_5d": 0.03, "ret_20d": 0.08, "ret_60d": 0.22,
             "vol_ratio": 1.5, "rsi": 62, "above_ma20": True, "above_ma60": True, "foreign_buy_5d": 5000, "bb_position": 0.65},
            {"stock_id": "3661", "stock_name": "世芯-KY", "ret_5d": 0.12, "ret_20d": 0.30, "ret_60d": 0.55,
             "vol_ratio": 2.8, "rsi": 78, "above_ma20": True, "above_ma60": True, "foreign_buy_5d": 3000, "bb_position": 0.88},
            {"stock_id": "2382", "stock_name": "廣達",    "ret_5d": 0.01, "ret_20d": 0.04, "ret_60d": 0.10,
             "vol_ratio": 1.1, "rsi": 52, "above_ma20": True, "above_ma60": False, "foreign_buy_5d": 1000, "bb_position": 0.50},
        ]

    results = []
    for s in stocks:
        try:
            r = await analyze_timeline(s)
            results.append(r)
        except Exception as e:
            logger.warning("[timeline] skip %s: %s", s.get("stock_id"), e)

    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


def format_timeline_summary(results: list[TimelineResult]) -> str:
    """格式化週期分佈摘要給 LINE"""
    if not results:
        return "⚠️ 暫無週期資料"

    stage_count: dict[CycleStage, list[str]] = {}
    for r in results:
        if r.stage not in stage_count:
            stage_count[r.stage] = []
        stage_count[r.stage].append(r.stock_id)

    lines = ["📅 市場週期分佈"]
    for stage in CycleStage:
        stocks = stage_count.get(stage, [])
        if stocks:
            emoji = STAGE_EMOJI[stage]
            zh = STAGE_ZH[stage]
            stock_str = "、".join(stocks[:5])
            if len(stocks) > 5:
                stock_str += f" +{len(stocks)-5}"
            lines.append(f"{emoji} {zh}({len(stocks)}): {stock_str}")

    return "\n".join(lines)
