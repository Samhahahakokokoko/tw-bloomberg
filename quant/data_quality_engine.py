"""
data_quality_engine.py — 資料品質評估引擎

每筆資料都帶 DataRecord 可信度標籤，
confidence < 0.7 → 標記不可靠
is_mock = True   → 禁止參與決策
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

# ── 資料新鮮度閾值（秒）──────────────────────────────────────────────────────
STALE_THRESHOLDS = {
    "tick":       5,          # 即時報價：5 秒
    "intraday":   300,        # 盤中資料：5 分鐘
    "daily":      86400,      # 日線：1 天
    "institutional": 86400,  # 法人資料：1 天
    "financial":  7776000,    # 財報：90 天
    "news":       21600,      # 新聞情緒：6 小時
    "analyst":    604800,     # 分析師資料：7 天
}

# ── 資料來源可靠度基準分 ──────────────────────────────────────────────────────
SOURCE_RELIABILITY: dict[str, float] = {
    "TWSE":         0.99,
    "TPEX":         0.98,
    "FinMind":      0.92,
    "MIS":          0.95,
    "Yahoo":        0.80,
    "Mock":         0.00,
    "mock":         0.00,
    "manual":       0.70,
    "calculated":   0.85,
    "unknown":      0.50,
}

CONFIDENCE_THRESHOLD = 0.70   # 低於此值標記不可靠


@dataclass
class DataRecord:
    value:       object         # 實際數值
    source:      str            # 資料來源名稱
    data_type:   str            # tick / daily / institutional / financial / news / analyst
    fetched_at:  str            # ISO timestamp
    is_mock:     bool = False
    latency_sec: Optional[float] = None
    confidence:  float = 1.0
    stale:       bool = False
    stale_reason: Optional[str] = None
    completeness: float = 1.0   # 0-1，欄位填充率
    _computed:   bool = False

    def compute(self) -> "DataRecord":
        if self._computed:
            return self
        self._computed = True

        # 1. Mock → 完全不可信
        if self.is_mock or self.source.lower() in ("mock", ""):
            self.confidence  = 0.0
            self.stale       = True
            self.stale_reason = "mock_data"
            return self

        # 2. 新鮮度分數
        threshold = STALE_THRESHOLDS.get(self.data_type, 86400)
        try:
            fetched = datetime.fromisoformat(self.fetched_at)
            age_sec = (datetime.now() - fetched).total_seconds()
        except Exception:
            age_sec = threshold * 2

        if age_sec > threshold:
            self.stale = True
            self.stale_reason = f"age_{int(age_sec)}s_threshold_{threshold}s"

        freshness_score = max(0.0, 1.0 - age_sec / (threshold * 3))

        # 3. 來源可靠度
        source_rel = SOURCE_RELIABILITY.get(self.source, 0.5)

        # 4. 完整度分數（由呼叫端設定，預設 1.0）
        completeness = max(0.0, min(1.0, self.completeness))

        # 5. 綜合信心分數
        self.confidence = round(
            freshness_score * 0.40 +
            source_rel      * 0.30 +
            completeness    * 0.30,
            4
        )
        return self

    @property
    def reliable(self) -> bool:
        self.compute()
        return (not self.is_mock) and (self.confidence >= CONFIDENCE_THRESHOLD)

    @property
    def age_seconds(self) -> float:
        try:
            return (datetime.now() - datetime.fromisoformat(self.fetched_at)).total_seconds()
        except Exception:
            return 9999.0

    def to_dict(self) -> dict:
        self.compute()
        return {
            "value":       self.value,
            "source":      self.source,
            "data_type":   self.data_type,
            "fetched_at":  self.fetched_at,
            "is_mock":     self.is_mock,
            "latency_sec": self.latency_sec,
            "confidence":  self.confidence,
            "stale":       self.stale,
            "stale_reason": self.stale_reason,
            "reliable":    self.reliable,
        }


def make_record(
    value: object,
    source: str,
    data_type: str,
    fetched_at: Optional[str] = None,
    is_mock: bool = False,
    latency_sec: Optional[float] = None,
    completeness: float = 1.0,
) -> DataRecord:
    """建立並計算 DataRecord（主要工廠函數）"""
    r = DataRecord(
        value        = value,
        source       = source,
        data_type    = data_type,
        fetched_at   = fetched_at or datetime.now().isoformat(),
        is_mock      = is_mock,
        latency_sec  = latency_sec,
        completeness = completeness,
    )
    r.compute()
    return r


def make_mock_record(value: object, data_type: str = "daily") -> DataRecord:
    """建立明確標記為 mock 的記錄"""
    return make_record(value, source="Mock", data_type=data_type, is_mock=True)


def aggregate_confidence(records: list[DataRecord]) -> float:
    """計算一組 DataRecord 的加權平均信心分數"""
    if not records:
        return 0.0
    scores = [r.confidence for r in records]
    return round(sum(scores) / len(scores), 4)


def has_mock(records: list[DataRecord]) -> bool:
    return any(r.is_mock for r in records)


def all_reliable(records: list[DataRecord]) -> bool:
    return all(r.reliable for r in records)


def stale_count(records: list[DataRecord]) -> int:
    return sum(1 for r in records if r.stale)


# ── 非同步資料庫記錄（選用）────────────────────────────────────────────────────

async def log_quality(
    stock_id: str,
    data_type: str,
    source: str,
    confidence: float,
    is_mock: bool,
    stale: bool,
    stale_reason: Optional[str] = None,
):
    """將資料品質記錄寫入 DB（若失敗靜默忽略，不阻塞主流程）"""
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import DataQualityLog
        async with AsyncSessionLocal() as db:
            db.add(DataQualityLog(
                stock_id     = stock_id,
                data_type    = data_type,
                source       = source,
                confidence   = confidence,
                is_mock      = is_mock,
                stale        = stale,
                stale_reason = stale_reason,
            ))
            await db.commit()
    except Exception:
        pass
