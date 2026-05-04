"""
market_data_validator.py — 市場資料合理性驗證

每筆進入系統的市場資料都要通過這裡：
  VALID   → 正常使用
  WARNING → 使用但降低 confidence
  INVALID → 拒絕，不進入系統
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# 台股漲跌停 ±10% + 0.5% 緩衝
LIMIT_UP_PCT    =  0.105
LIMIT_DOWN_PCT  = -0.105
GAP_ALERT_PCT   =  0.15   # 跳空 > 15% → 需確認
VOLUME_SPIKE_X  = 10.0    # 量能爆增 10x → 標記
INST_MAX_PCT    = 0.50    # 法人買賣超 > 市值50% → 拒絕


class ValidationStatus(str, Enum):
    VALID   = "VALID"
    WARNING = "WARNING"
    INVALID = "INVALID"


@dataclass
class ValidationResult:
    status:      ValidationStatus
    issues:      list[str] = field(default_factory=list)
    warnings:    list[str] = field(default_factory=list)
    confidence_penalty: float = 0.0   # 從原始 confidence 扣除的量

    @property
    def ok(self) -> bool:
        return self.status != ValidationStatus.INVALID

    def to_dict(self) -> dict:
        return {
            "status":             self.status.value,
            "issues":             self.issues,
            "warnings":           self.warnings,
            "confidence_penalty": self.confidence_penalty,
        }


def validate_ohlcv(
    stock_id:   str,
    open_p:     Optional[float],
    high_p:     Optional[float],
    low_p:      Optional[float],
    close_p:    Optional[float],
    volume:     Optional[float],
    prev_close: Optional[float] = None,
    is_trading_day: bool = True,
) -> ValidationResult:
    """OHLCV 資料合理性驗證"""
    issues:   list[str] = []
    warnings: list[str] = []
    penalty   = 0.0

    # ── None / 零值檢查 ───────────────────────────────────────────────────────
    if close_p is None or close_p <= 0:
        issues.append(f"close_price_invalid_{close_p}")
        return ValidationResult(ValidationStatus.INVALID, issues, warnings, 1.0)

    if high_p is None or low_p is None or open_p is None:
        warnings.append("ohlc_incomplete")
        penalty += 0.10
    else:
        # ── OHLC 邏輯關係 ────────────────────────────────────────────────────
        if high_p < low_p:
            issues.append(f"high_{high_p}_lt_low_{low_p}")
            return ValidationResult(ValidationStatus.INVALID, issues, warnings, 1.0)

        if close_p > high_p:
            issues.append(f"close_{close_p}_gt_high_{high_p}")
            return ValidationResult(ValidationStatus.INVALID, issues, warnings, 1.0)

        if close_p < low_p:
            issues.append(f"close_{close_p}_lt_low_{low_p}")
            return ValidationResult(ValidationStatus.INVALID, issues, warnings, 1.0)

        if open_p <= 0:
            warnings.append(f"open_price_zero")
            penalty += 0.05

    # ── 漲跌幅檢查 ───────────────────────────────────────────────────────────
    if prev_close and prev_close > 0:
        change_pct = (close_p - prev_close) / prev_close
        if change_pct > LIMIT_UP_PCT or change_pct < LIMIT_DOWN_PCT:
            warnings.append(f"change_pct_{change_pct:.3f}_exceeds_limit")
            penalty += 0.15
        if abs(change_pct) > GAP_ALERT_PCT:
            warnings.append(f"gap_alert_{change_pct:.3f}_needs_verification")
            penalty += 0.10

    # ── 成交量檢查 ───────────────────────────────────────────────────────────
    if volume is not None:
        if volume == 0 and is_trading_day:
            warnings.append("zero_volume_on_trading_day")
            penalty += 0.20
    else:
        warnings.append("volume_missing")
        penalty += 0.10

    if issues:
        status = ValidationStatus.INVALID
    elif warnings:
        status = ValidationStatus.WARNING
    else:
        status = ValidationStatus.VALID

    return ValidationResult(status, issues, warnings, min(1.0, penalty))


def validate_volume_spike(
    stock_id:    str,
    current_vol: float,
    avg_vol_20d: float,
) -> ValidationResult:
    """成交量異常爆增檢查"""
    if avg_vol_20d <= 0:
        return ValidationResult(ValidationStatus.WARNING, [], ["avg_volume_missing"], 0.05)

    ratio = current_vol / avg_vol_20d
    if ratio > VOLUME_SPIKE_X:
        return ValidationResult(
            ValidationStatus.WARNING,
            [],
            [f"volume_spike_{ratio:.1f}x_avg"],
            0.10,
        )
    return ValidationResult(ValidationStatus.VALID)


def validate_institutional(
    stock_id:      str,
    foreign_net:   float,    # 外資淨買（張）
    trust_net:     float,    # 投信淨買（張）
    market_cap_k:  float,    # 市值（千張）
    data_date:     Optional[str] = None,
    latest_trading_day: Optional[str] = None,
) -> ValidationResult:
    """法人資料合理性驗證"""
    issues:   list[str] = []
    warnings: list[str] = []
    penalty   = 0.0

    # 買賣超金額不合理（超過市值 50%）
    if market_cap_k > 0:
        foreign_pct = abs(foreign_net) / market_cap_k
        trust_pct   = abs(trust_net)   / market_cap_k
        if foreign_pct > INST_MAX_PCT:
            issues.append(f"foreign_net_exceeds_50pct_market_cap_{foreign_pct:.2%}")
        if trust_pct > INST_MAX_PCT:
            issues.append(f"trust_net_exceeds_50pct_market_cap_{trust_pct:.2%}")

    # 資料日期是否為最新交易日
    if data_date and latest_trading_day:
        if data_date < latest_trading_day:
            warnings.append(f"institutional_date_{data_date}_not_latest_{latest_trading_day}")
            penalty += 0.15

    if issues:
        return ValidationResult(ValidationStatus.INVALID, issues, warnings, 1.0)
    if warnings:
        return ValidationResult(ValidationStatus.WARNING, issues, warnings, penalty)
    return ValidationResult(ValidationStatus.VALID)


def validate_batch(records: list[dict]) -> dict[str, ValidationResult]:
    """
    批次驗證多筆資料。
    records: [{"stock_id": ..., "type": "ohlcv"|"institutional", ...}]
    """
    results: dict[str, ValidationResult] = {}
    for r in records:
        sid  = r.get("stock_id", "")
        rtype = r.get("type", "ohlcv")
        try:
            if rtype == "ohlcv":
                result = validate_ohlcv(
                    sid,
                    r.get("open"),
                    r.get("high"),
                    r.get("low"),
                    r.get("close"),
                    r.get("volume"),
                    r.get("prev_close"),
                    r.get("is_trading_day", True),
                )
            elif rtype == "institutional":
                result = validate_institutional(
                    sid,
                    r.get("foreign_net", 0),
                    r.get("trust_net", 0),
                    r.get("market_cap_k", 0),
                    r.get("data_date"),
                    r.get("latest_trading_day"),
                )
            else:
                result = ValidationResult(ValidationStatus.WARNING, [], ["unknown_type"])
        except Exception as e:
            logger.warning("[Validator] %s %s failed: %s", sid, rtype, e)
            result = ValidationResult(ValidationStatus.WARNING, [], [f"validation_exception_{type(e).__name__}"], 0.20)

        results[sid] = result
        if result.status == ValidationStatus.INVALID:
            logger.warning("[Validator] INVALID %s: %s", sid, result.issues)

    return results
