"""
audit_log_engine.py — 決策稽核日誌

記錄每個決策的完整過程：
  - 輸入資料來源與品質
  - 每個 Layer 的結果
  - 最終決策與信心分數
  - 資料品質摘要

寫入 audit_logs 資料表，可供事後追查。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AuditEntry:
    stock_id:         str
    action:           str           # buy / sell / watch / skipped / blocked
    confidence:       float
    reasons:          list[str]     = field(default_factory=list)
    layer_results:    dict          = field(default_factory=dict)
    data_sources:     list[str]     = field(default_factory=list)
    avg_confidence:   float = 0.0
    mock_count:       int   = 0
    stale_count:      int   = 0
    eligible:         bool  = True
    blocking_reasons: list[str] = field(default_factory=list)
    kill_switch_on:   bool  = False
    ts:               str   = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "stock_id":         self.stock_id,
            "action":           self.action,
            "confidence":       round(self.confidence, 2),
            "reasons":          self.reasons,
            "layer_results":    self.layer_results,
            "data_sources":     self.data_sources,
            "avg_confidence":   round(self.avg_confidence, 4),
            "mock_count":       self.mock_count,
            "stale_count":      self.stale_count,
            "eligible":         self.eligible,
            "blocking_reasons": self.blocking_reasons,
            "kill_switch_on":   self.kill_switch_on,
            "ts":               self.ts,
        }


class AuditLogger:
    """
    輕量稽核日誌器。
    在 decision_engine.run() 結束前呼叫 flush() 批次寫入 DB。
    """

    def __init__(self):
        self._entries: list[AuditEntry] = []
        self._session_id: str = datetime.now().strftime("%Y%m%d_%H%M%S")

    def record(self, entry: AuditEntry):
        self._entries.append(entry)
        logger.debug(
            "[Audit] %s %s conf=%.1f eligible=%s%s",
            entry.stock_id, entry.action, entry.confidence,
            entry.eligible,
            f" BLOCKED={entry.blocking_reasons}" if entry.blocking_reasons else "",
        )

    def record_skip(self, stock_id: str, reason: str, kill_switch: bool = False):
        self.record(AuditEntry(
            stock_id       = stock_id,
            action         = "skipped",
            confidence     = 0.0,
            blocking_reasons = [reason],
            kill_switch_on = kill_switch,
        ))

    def record_decision(
        self,
        stock_id:       str,
        action:         str,
        confidence:     float,
        reasons:        list[str],
        layer_results:  dict | None = None,
        data_quality:   dict | None = None,
        eligible:       bool = True,
        blocking:       list[str] | None = None,
    ):
        dq = data_quality or {}
        self.record(AuditEntry(
            stock_id         = stock_id,
            action           = action,
            confidence       = confidence,
            reasons          = reasons,
            layer_results    = layer_results or {},
            data_sources     = dq.get("sources", []),
            avg_confidence   = dq.get("avg_confidence", 0.0),
            mock_count       = dq.get("mock_count", 0),
            stale_count      = dq.get("stale_count", 0),
            eligible         = eligible,
            blocking_reasons = blocking or [],
        ))

    async def flush(self):
        """批次寫入 DB"""
        if not self._entries:
            return
        try:
            from backend.models.database import AsyncSessionLocal
            from backend.models.models import AuditLog
            async with AsyncSessionLocal() as db:
                for e in self._entries:
                    db.add(AuditLog(
                        session_id       = self._session_id,
                        stock_id         = e.stock_id,
                        action           = e.action,
                        confidence       = e.confidence,
                        reasons_json     = json.dumps(e.reasons, ensure_ascii=False),
                        layer_results_json = json.dumps(e.layer_results, ensure_ascii=False),
                        avg_data_confidence = e.avg_confidence,
                        mock_count       = e.mock_count,
                        stale_count      = e.stale_count,
                        eligible         = e.eligible,
                        blocking_reasons_json = json.dumps(e.blocking_reasons, ensure_ascii=False),
                        kill_switch_on   = e.kill_switch_on,
                    ))
                await db.commit()
            logger.info("[Audit] flushed %d entries (session=%s)", len(self._entries), self._session_id)
        except Exception as e:
            logger.warning("[Audit] flush failed: %s", e)
        finally:
            self._entries.clear()

    def summary(self) -> dict:
        total    = len(self._entries)
        skipped  = sum(1 for e in self._entries if e.action == "skipped")
        blocked  = sum(1 for e in self._entries if not e.eligible)
        buy_sell = sum(1 for e in self._entries if e.action in ("buy", "add", "sell", "reduce"))
        return {
            "session_id": self._session_id,
            "total":      total,
            "skipped":    skipped,
            "blocked":    blocked,
            "decisions":  buy_sell,
        }
