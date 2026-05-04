"""
trade_eligibility_engine.py — 交易資格判斷引擎

每檔股票在進入 decision_engine 前必須通過資格審查。
任一禁止條件觸發 → trade_eligible = False → 跳過該股
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .data_quality_engine import DataRecord, CONFIDENCE_THRESHOLD

logger = logging.getLogger(__name__)

# ── 閾值設定 ─────────────────────────────────────────────────────────────────
PRICE_CONFIDENCE_MIN   = 0.60   # 價格資料最低信心
INST_STALE_DAYS        = 3      # 法人資料超過幾天算過時
FINANCIAL_STALE_DAYS   = 120    # 財務資料超過幾天算過時
OVERALL_CONFIDENCE_MIN = 0.60   # 整體信心最低門檻
MIN_VOLUME_K           = 500    # 最低日均量（張）


@dataclass
class EligibilityResult:
    symbol:           str
    trade_eligible:   bool
    confidence_score: float           # 0-1
    blocking_reasons: list[str] = field(default_factory=list)
    warnings:         list[str]  = field(default_factory=list)
    records_checked:  int = 0
    ts:               str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "symbol":           self.symbol,
            "trade_eligible":   self.trade_eligible,
            "confidence_score": round(self.confidence_score, 4),
            "blocking_reasons": self.blocking_reasons,
            "warnings":         self.warnings,
        }

    def log_line(self) -> str:
        status = "✅ ELIGIBLE" if self.trade_eligible else "⛔ BLOCKED"
        r = f"  blocking={self.blocking_reasons}" if self.blocking_reasons else ""
        w = f"  warn={self.warnings}"              if self.warnings         else ""
        return f"{status} {self.symbol} conf={self.confidence_score:.2f}{r}{w}"


def check_eligibility(
    symbol: str,
    price_record:    Optional[DataRecord] = None,
    inst_record:     Optional[DataRecord] = None,
    financial_record: Optional[DataRecord] = None,
    volume_k:        float = 0,
    extra_records:   list[DataRecord] | None = None,
) -> EligibilityResult:
    """
    同步版本，decision_engine 直接呼叫。
    每個 DataRecord 由各引擎傳入；若為 None 則略過該檢查。
    """
    blocking: list[str] = []
    warnings: list[str] = []
    all_records: list[DataRecord] = []

    # ── 價格資料 ──────────────────────────────────────────────────────────────
    if price_record is not None:
        all_records.append(price_record)
        if price_record.is_mock:
            blocking.append("price_data_is_mock")
        elif price_record.confidence < PRICE_CONFIDENCE_MIN:
            blocking.append(f"price_data_confidence_low_{price_record.confidence:.2f}")
        elif price_record.stale:
            warnings.append(f"price_data_stale_{price_record.stale_reason}")

    # ── 法人資料 ──────────────────────────────────────────────────────────────
    if inst_record is not None:
        all_records.append(inst_record)
        if inst_record.is_mock:
            blocking.append("institutional_data_is_mock")
        elif inst_record.age_seconds > INST_STALE_DAYS * 86400:
            blocking.append(f"institutional_data_stale_{int(inst_record.age_seconds//3600)}h")
        elif inst_record.stale:
            warnings.append("institutional_data_stale")
    else:
        warnings.append("institutional_data_missing")

    # ── 財務資料 ──────────────────────────────────────────────────────────────
    if financial_record is not None:
        all_records.append(financial_record)
        if financial_record.is_mock:
            blocking.append("financial_data_is_mock")
        elif financial_record.age_seconds > FINANCIAL_STALE_DAYS * 86400:
            blocking.append(f"financial_data_stale_{int(financial_record.age_seconds//86400)}days")
        elif financial_record.stale:
            warnings.append("financial_data_stale")
    else:
        warnings.append("financial_data_missing")

    # ── 額外資料 ──────────────────────────────────────────────────────────────
    if extra_records:
        all_records.extend(extra_records)

    # ── 整體信心 ──────────────────────────────────────────────────────────────
    if all_records:
        avg_conf = sum(r.confidence for r in all_records) / len(all_records)
    else:
        avg_conf = 0.5
        warnings.append("no_data_records_provided")

    if avg_conf < OVERALL_CONFIDENCE_MIN:
        blocking.append(f"overall_confidence_too_low_{avg_conf:.2f}")

    # ── 流動性 ────────────────────────────────────────────────────────────────
    if 0 < volume_k < MIN_VOLUME_K:
        warnings.append(f"volume_below_threshold_{volume_k:.0f}k")

    trade_eligible = len(blocking) == 0

    result = EligibilityResult(
        symbol           = symbol,
        trade_eligible   = trade_eligible,
        confidence_score = avg_conf,
        blocking_reasons = blocking,
        warnings         = warnings,
        records_checked  = len(all_records),
    )

    if not trade_eligible:
        logger.info("[Eligibility] %s", result.log_line())

    return result


async def check_eligibility_async(symbol: str, **kwargs) -> EligibilityResult:
    """非同步版本，自動從 DB 補充資料品質記錄"""
    result = check_eligibility(symbol, **kwargs)

    # 寫入 DB 日誌
    try:
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import TradeEligibilityLog
        async with AsyncSessionLocal() as db:
            import json
            db.add(TradeEligibilityLog(
                stock_id         = symbol,
                trade_eligible   = result.trade_eligible,
                confidence_score = result.confidence_score,
                blocking_reasons = json.dumps(result.blocking_reasons, ensure_ascii=False),
                warnings         = json.dumps(result.warnings, ensure_ascii=False),
            ))
            await db.commit()
    except Exception:
        pass

    return result


def bulk_eligibility(
    stocks: list[dict],
) -> dict[str, EligibilityResult]:
    """
    批次審查多檔股票。
    stocks: [{"symbol": "2330", "price_record": DataRecord, ...}]
    回傳: {symbol: EligibilityResult}
    """
    results: dict[str, EligibilityResult] = {}
    for s in stocks:
        sym = s.get("symbol", "")
        if not sym:
            continue
        results[sym] = check_eligibility(
            symbol           = sym,
            price_record     = s.get("price_record"),
            inst_record      = s.get("inst_record"),
            financial_record = s.get("financial_record"),
            volume_k         = s.get("volume_k", 0),
            extra_records    = s.get("extra_records"),
        )
    return results
