"""
backtest_integrity_engine.py — 回測資料完整性驗證

防止三大偏差：
  1. Lookahead Bias  — 使用未來資料
  2. Survivorship Bias — 只用存活股
  3. Data Leakage    — train/test 資料污染
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class IntegrityReport:
    lookahead_bias_detected:   bool = False
    survivorship_bias_risk:    str  = "UNKNOWN"   # LOW / MEDIUM / HIGH
    data_leakage_detected:     bool = False
    mock_data_in_backtest:     bool = False
    feature_ts_violations:     list[str] = field(default_factory=list)
    delisted_stocks_included:  int  = 0
    delisted_stocks_excluded:  int  = 0
    train_test_overlap_days:   int  = 0
    integrity_score:           float = 1.0
    warnings:                  list[str] = field(default_factory=list)
    ts:                        str = field(default_factory=lambda: datetime.now().isoformat())

    def compute_score(self) -> "IntegrityReport":
        score = 1.0
        if self.lookahead_bias_detected:  score -= 0.40
        if self.data_leakage_detected:    score -= 0.30
        if self.mock_data_in_backtest:    score -= 0.20
        if self.survivorship_bias_risk == "HIGH":   score -= 0.20
        elif self.survivorship_bias_risk == "MEDIUM": score -= 0.10
        if self.train_test_overlap_days > 0:  score -= 0.15
        score -= len(self.feature_ts_violations) * 0.05
        self.integrity_score = max(0.0, round(score, 3))
        return self

    def to_dict(self) -> dict:
        return {
            "lookahead_bias_detected":  self.lookahead_bias_detected,
            "survivorship_bias_risk":   self.survivorship_bias_risk,
            "data_leakage_detected":    self.data_leakage_detected,
            "mock_data_in_backtest":    self.mock_data_in_backtest,
            "feature_ts_violations":    self.feature_ts_violations,
            "delisted_included":        self.delisted_stocks_included,
            "delisted_excluded":        self.delisted_stocks_excluded,
            "train_test_overlap_days":  self.train_test_overlap_days,
            "integrity_score":          self.integrity_score,
            "warnings":                 self.warnings,
        }

    def format_line(self) -> str:
        score_bar = "█" * int(self.integrity_score * 10) + "░" * (10 - int(self.integrity_score * 10))
        lines = [
            f"🔍 回測完整性報告",
            f"完整性分數：[{score_bar}] {self.integrity_score:.2f}",
            f"前視偏差：{'❌ 偵測到' if self.lookahead_bias_detected else '✅ 無'}",
            f"存活者偏差：{'⚠️ ' if self.survivorship_bias_risk != 'LOW' else '✅ '}{self.survivorship_bias_risk}",
            f"資料洩漏：{'❌ 偵測到' if self.data_leakage_detected else '✅ 無'}",
            f"假資料混入：{'⚠️ 有' if self.mock_data_in_backtest else '✅ 無'}",
        ]
        if self.warnings:
            lines.append("")
            for w in self.warnings[:4]:
                lines.append(f"  ⚠️ {w}")
        return "\n".join(lines)


def check_lookahead_bias(
    feature_timestamps: list[str],
    target_timestamps:  list[str],
) -> tuple[bool, list[str]]:
    """
    驗證特徵時間戳記是否嚴格早於目標時間戳記。
    feature_timestamps[i] 必須 < target_timestamps[i]
    """
    violations = []
    for i, (ft, tt) in enumerate(zip(feature_timestamps, target_timestamps)):
        try:
            if ft >= tt:
                violations.append(f"row_{i}: feature_ts={ft} >= target_ts={tt}")
        except Exception:
            pass
    return len(violations) > 0, violations[:10]  # 最多回報 10 個


def check_survivorship_bias(
    backtest_stocks:  list[str],
    universe_stocks:  list[str],
    delisted_stocks:  list[str],
) -> tuple[str, int, int]:
    """
    評估存活者偏差風險。
    回傳 (risk_level, delisted_included, delisted_excluded)
    """
    backtest_set  = set(backtest_stocks)
    delisted_set  = set(delisted_stocks)

    included  = len(backtest_set & delisted_set)
    excluded  = len(delisted_set - backtest_set)

    total_delisted = len(delisted_set)
    if total_delisted == 0:
        risk = "MEDIUM"   # 無已下市股資料 → 中風險（可能根本沒有）
    else:
        pct_excluded = excluded / total_delisted
        if pct_excluded > 0.8:
            risk = "HIGH"
        elif pct_excluded > 0.3:
            risk = "MEDIUM"
        else:
            risk = "LOW"

    return risk, included, excluded


def check_data_leakage(
    train_end_date:  str,
    test_start_date: str,
    scaler_fitted_on: str = "train",  # "train" / "full" / "test"
) -> tuple[bool, list[str]]:
    """
    驗證 train/test 分割是否嚴格。
    """
    warnings = []
    leakage  = False

    try:
        if train_end_date >= test_start_date:
            leakage = True
            warnings.append(f"train_end={train_end_date} overlaps test_start={test_start_date}")
    except Exception:
        pass

    if scaler_fitted_on in ("full", "test"):
        leakage = True
        warnings.append(f"scaler_fitted_on={scaler_fitted_on} (should be train only)")

    return leakage, warnings


def check_mock_in_backtest(prices: list[dict]) -> bool:
    """檢查回測資料中是否有 mock 標記"""
    return any(
        p.get("is_mock", False) or p.get("source", "").lower() == "mock"
        for p in prices
    )


def run_integrity_check(
    feature_timestamps:  list[str]  = None,
    target_timestamps:   list[str]  = None,
    backtest_stocks:     list[str]  = None,
    universe_stocks:     list[str]  = None,
    delisted_stocks:     list[str]  = None,
    train_end_date:      str        = "",
    test_start_date:     str        = "",
    scaler_fitted_on:    str        = "train",
    price_records:       list[dict] = None,
) -> IntegrityReport:
    """完整完整性審查"""
    report = IntegrityReport()

    # 1. Lookahead Bias
    if feature_timestamps and target_timestamps:
        detected, violations = check_lookahead_bias(feature_timestamps, target_timestamps)
        report.lookahead_bias_detected  = detected
        report.feature_ts_violations    = violations

    # 2. Survivorship Bias
    if backtest_stocks is not None:
        risk, inc, exc = check_survivorship_bias(
            backtest_stocks,
            universe_stocks  or [],
            delisted_stocks  or [],
        )
        report.survivorship_bias_risk   = risk
        report.delisted_stocks_included = inc
        report.delisted_stocks_excluded = exc

    # 3. Data Leakage
    if train_end_date and test_start_date:
        detected, warnings = check_data_leakage(train_end_date, test_start_date, scaler_fitted_on)
        report.data_leakage_detected = detected
        report.warnings.extend(warnings)

    # 4. Mock Data
    if price_records:
        report.mock_data_in_backtest = check_mock_in_backtest(price_records)
        if report.mock_data_in_backtest:
            report.warnings.append("mock_price_data_detected_in_backtest")

    report.compute_score()
    logger.info("[Integrity] score=%.3f lookahead=%s survivorship=%s leakage=%s",
                report.integrity_score,
                report.lookahead_bias_detected,
                report.survivorship_bias_risk,
                report.data_leakage_detected)
    return report


async def save_integrity_report(session_id: str, report: IntegrityReport):
    """儲存回測完整性報告到 DB"""
    try:
        import json
        from backend.models.database import AsyncSessionLocal
        from backend.models.models import BacktestIntegrityLog
        async with AsyncSessionLocal() as db:
            db.add(BacktestIntegrityLog(
                session_id              = session_id,
                lookahead_bias          = report.lookahead_bias_detected,
                survivorship_bias_risk  = report.survivorship_bias_risk,
                data_leakage            = report.data_leakage_detected,
                mock_data_in_backtest   = report.mock_data_in_backtest,
                integrity_score         = report.integrity_score,
                warnings_json           = json.dumps(report.warnings, ensure_ascii=False),
            ))
            await db.commit()
    except Exception as e:
        logger.warning("[Integrity] save failed: %s", e)
